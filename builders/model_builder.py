import functools
import logging

import numpy as np
import tensorflow as tf

from models import unet
from utils import standard_fields
from utils import preprocessor
from utils import session_hooks
from utils import metric_utils
from utils import image_utils
from utils import util_ops
from builders import optimizer_builder
from builders import activation_fn_builder


def _extract_patient_id(file_name):
  tokens = file_name.split('/')
  assert(tokens[-3] == 'healthy_cases' or tokens[-3] == 'cancer_cases')
  is_healthy = tokens[-3] == 'healthy_cases'

  patient_id_prefix = 'h' if is_healthy else 'c'
  patient_id = patient_id_prefix + tokens[-2]

  return patient_id


def _loss(labels, logits, loss_name, pos_weight):
  # Each entry in labels must be an index in [0, num_classes)
  assert(len(labels.get_shape()) == 1)

  if loss_name == 'sigmoid':
    assert(logits.get_shape().as_list()[1] == 1)
    return tf.reduce_mean(tf.nn.weighted_cross_entropy_with_logits(
      tf.to_float(labels), tf.squeeze(logits, axis=1), pos_weight))
  elif loss_name == 'softmax':
    # Logits should be of shape [batch_size, num_classes]
    assert(len(logits.get_shape()) == 2)
    assert(pos_weight is None and 'Loss weight not implemented for softmax.')
    return tf.losses.sparse_softmax_cross_entropy(labels, logits)
  else:
    assert(False and 'Loss name "{}" not recognized.'.format(loss_name))


def _general_model_fn(features, pipeline_config, result_folder, dataset_info,
                      feature_extractor, mode, num_gpu,
                      visualization_file_names, eval_dir):
  num_classes = pipeline_config.dataset.num_classes
  add_background_class = pipeline_config.train_config.loss.name == 'softmax'
  if add_background_class:
    assert(num_classes == 1)
    num_classes += 1

  image_batch = features[standard_fields.InputDataFields.image_decoded]

  if mode == tf.estimator.ModeKeys.PREDICT:
    annotation_mask_batch = None
  else:
    annotation_mask_batch = features[
      standard_fields.InputDataFields.annotation_mask]

  if mode == tf.estimator.ModeKeys.TRAIN:
    # Augment images
    image_batch, annotation_mask_batch = preprocessor.apply_data_augmentation(
      pipeline_config.train_config.data_augmentation_options,
      images=image_batch, gt_masks=annotation_mask_batch,
      batch_size=pipeline_config.train_config.batch_size)

  # General preprocessing
  image_batch_preprocessed = preprocessor.preprocess(
    image_batch, pipeline_config.dataset.val_range,
    scale_input=pipeline_config.dataset.scale_input)

  network_output = feature_extractor.build_network(
    image_batch_preprocessed, is_training=mode == tf.estimator.ModeKeys.TRAIN,
    num_classes=num_classes,
    use_batch_norm=pipeline_config.model.use_batch_norm,
    bn_momentum=pipeline_config.model.batch_norm_momentum,
    bn_epsilon=pipeline_config.model.batch_norm_epsilon,
    activation_fn=activation_fn_builder.build(pipeline_config.model))

  if mode == tf.estimator.ModeKeys.TRAIN:
    # Record model variable summaries
    for var in tf.trainable_variables():
      tf.summary.histogram('ModelVars/' + var.op.name, var)

  network_output_shape = network_output.get_shape().as_list()
  if mode != tf.estimator.ModeKeys.PREDICT:
    if (network_output_shape[1:3]
        != annotation_mask_batch.get_shape().as_list()[1:3]):
      annotation_mask_batch = image_utils.central_crop(
        annotation_mask_batch,
        desired_size=network_output.get_shape().as_list()[1:3])

    annotation_mask_batch = tf.cast(
      tf.clip_by_value(annotation_mask_batch, 0, 1), dtype=tf.int64)

    assert(len(annotation_mask_batch.get_shape()) == 4)
    assert(annotation_mask_batch.get_shape().as_list()[:3]
           == network_output.get_shape().as_list()[:3])

  # We should not apply the loss to evaluation. This would just cause
  # our loss to be minimum for f2 score, but we also get the same
  # optimum if we just optimzie for f1 score
  if (pipeline_config.train_config.loss.use_weighted
      and mode == tf.estimator.ModeKeys.TRAIN):
    patient_ratio = dataset_info[
      standard_fields.PickledDatasetInfo.patient_ratio]
    cancer_pixels = tf.reduce_sum(tf.to_float(annotation_mask_batch))
    healthy_pixels = tf.to_float(tf.size(
      annotation_mask_batch)) - cancer_pixels

    batch_pixel_ratio = tf.div(healthy_pixels, cancer_pixels + 1.0)

    loss_weight = (
      ((batch_pixel_ratio * patient_ratio)
       + pipeline_config.train_config.loss.weight_constant_add)
      * pipeline_config.train_config.loss.weight_constant_multiply)
  else:
    loss_weight = tf.constant(1.0)

  if mode == tf.estimator.ModeKeys.PREDICT:
    loss = None
  else:
    loss = _loss(tf.reshape(annotation_mask_batch, [-1]),
                 tf.reshape(network_output, [-1, num_classes]),
                 loss_name=pipeline_config.train_config.loss.name,
                 pos_weight=loss_weight)
    loss = tf.identity(loss, name='ModelLoss')
    tf.summary.scalar(loss.op.name, loss, family='Loss')

    total_loss = tf.identity(loss, name='TotalLoss')

    if mode == tf.estimator.ModeKeys.TRAIN:
      if pipeline_config.train_config.add_regularization_loss:
        regularization_losses = tf.get_collection(
          tf.GraphKeys.REGULARIZATION_LOSSES)
        if regularization_losses:
          regularization_loss = tf.add_n(regularization_losses,
                                         name='RegularizationLoss')
          total_loss = tf.add_n([loss, regularization_loss],
                                name='TotalLoss')
          tf.summary.scalar(regularization_loss.op.name, regularization_loss,
                            family='Loss')

    tf.summary.scalar(total_loss.op.name, total_loss, family='Loss')
    total_loss = tf.check_numerics(total_loss, 'LossTensor is inf or nan.')

  scaffold = None
  update_ops = []
  if mode == tf.estimator.ModeKeys.TRAIN:
    if pipeline_config.train_config.optimizer.use_moving_average:
      # EMA's are currently not supported with tf's DistributionStrategy.
      # Reenable once they fixed the bugs
      logging.warn(
        'EMA is currently not supported with tf DistributionStrategy.')
      exit(1)
      pipeline_config.train_config.optimizer.use_moving_average = False
      # The swapping saver will swap the trained variables with their moving
      # averages before saving, thus removing the need to care for moving
      # averages during evaluation
      # scaffold = tf.train.Scaffold(saver=optimizer.swapping_saver())

    optimizer, optimizer_summary_vars = optimizer_builder.build(
      pipeline_config.train_config.optimizer)
    for var in optimizer_summary_vars:
      tf.summary.scalar(var.op.name, var, family='LearningRate')

    grads_and_vars = optimizer.compute_gradients(total_loss)

    update_ops.append(optimizer.apply_gradients(
      grads_and_vars, global_step=tf.train.get_global_step()))

  graph_update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
  update_ops.append(graph_update_ops)
  update_op = tf.group(*update_ops, name='update_barrier')
  with tf.control_dependencies([update_op]):
    if mode == tf.estimator.ModeKeys.PREDICT:
      train_op = None
    else:
      train_op = tf.identity(total_loss)

  if mode == tf.estimator.ModeKeys.TRAIN:
    logging.info("Total number of trainable parameters: {}".format(np.sum([
      np.prod(v.get_shape().as_list()) for v in tf.trainable_variables()])))

    # Training Hooks are not working with MirroredStrategy. Fixed in 1.13
    #print_hook = session_hooks.PrintHook(
    #  file_name=features[standard_fields.InputDataFields.image_file],
    #  batch_pixel_ratio=batch_pixel_ratio)
    return tf.estimator.EstimatorSpec(mode,
                                      loss=total_loss, train_op=train_op,
                                      scaffold=scaffold)
  elif mode == tf.estimator.ModeKeys.EVAL:
    if pipeline_config.train_config.loss.name == 'sigmoid':
      scaled_network_output = tf.nn.sigmoid(network_output)[:, :, :, 0]
    elif pipeline_config.train_config.loss.name == 'softmax':
      assert(network_output.get_shape().as_list()[-1] == 2)
      scaled_network_output = tf.nn.softmax(network_output)[:, :, :, 1]

      # Metrics
    metric_dict, statistics_dict = metric_utils.get_metrics(
      scaled_network_output, annotation_mask_batch,
      tp_thresholds=np.array(pipeline_config.metrics_tp_thresholds,
                             dtype=np.float32),
      parallel_iterations=min(pipeline_config.eval_config.batch_size,
                              util_ops.get_cpu_count()))

    vis_hook = session_hooks.VisualizationHook(
      result_folder=result_folder,
      visualization_file_names=visualization_file_names,
      file_name=features[standard_fields.InputDataFields.image_file],
      image_decoded=image_batch,
      annotation_decoded=features[
        standard_fields.InputDataFields.annotation_decoded],
      predicted_mask=scaled_network_output, eval_dir=eval_dir)
    patient_metric_hook = session_hooks.PatientMetricHook(
      statistics_dict=statistics_dict,
      patient_id=features[standard_fields.InputDataFields.patient_id],
      result_folder=result_folder,
      tp_thresholds=pipeline_config.metrics_tp_thresholds, eval_dir=eval_dir)

    return tf.estimator.EstimatorSpec(
      mode, loss=total_loss, train_op=train_op,
      evaluation_hooks=[vis_hook, patient_metric_hook],
      eval_metric_ops=metric_dict)
  elif mode == tf.estimator.ModeKeys.PREDICT:
    if pipeline_config.train_config.loss.name == 'sigmoid':
      scaled_network_output = tf.nn.sigmoid(network_output)[:, :, :, 0]
    elif pipeline_config.train_config.loss.name == 'softmax':
      assert(network_output.get_shape().as_list()[-1] == 2)
      scaled_network_output = tf.nn.softmax(network_output)[:, :, :, 1]

    vis_hook = session_hooks.VisualizationHook(
      result_folder=result_folder,
      visualization_file_names=None,
      file_name=features[standard_fields.InputDataFields.image_file],
      image_decoded=image_batch,
      annotation_decoded=None,
      predicted_mask=scaled_network_output, eval_dir=eval_dir)

    predicted_mask = tf.stack([scaled_network_output * 255,
                               tf.zeros_like(scaled_network_output),
                              tf.zeros_like(scaled_network_output)], axis=3)

    predicted_mask_overlay = tf.clip_by_value(
      features[standard_fields.InputDataFields.image_decoded]
      * 0.5 + predicted_mask, 0, 255)

    return tf.estimator.EstimatorSpec(
      mode, prediction_hooks=[vis_hook], predictions={
        'image_file': features[standard_fields.InputDataFields.image_file],
        'prediction': predicted_mask_overlay})
  else:
    assert(False)


def get_model_fn(pipeline_config, result_folder, dataset_info,
                 eval_split_name, num_gpu, eval_dir):

  if dataset_info is None:
    visualization_file_names = None
  else:
    file_names = dataset_info[
      standard_fields.PickledDatasetInfo.file_names][eval_split_name]
    np.random.shuffle(file_names)

    patient_ids = dataset_info[
      standard_fields.PickledDatasetInfo.patient_ids][eval_split_name]

    # Select one image per patient
    selected_files = dict()
    for file_name in file_names:
      patient_id = _extract_patient_id(file_name)
      assert(patient_id in patient_ids)
      if patient_id not in selected_files:
        selected_files[patient_id] = file_name

    num_visualizations = pipeline_config.eval_config.num_images_to_visualize
    if num_visualizations is None or num_visualizations == -1:
      num_visualizations = len(selected_files)
    else:
      num_visualizations = min(num_visualizations,
                               len(selected_files))

    visualization_file_names = list(selected_files.values())[
      :num_visualizations]

  model_name = pipeline_config.model.WhichOneof('model_type')
  if model_name == 'unet':
    feature_extractor = unet.UNet(
      weight_decay=pipeline_config.train_config.weight_decay,
      conv_padding=pipeline_config.model.conv_padding,
      filter_sizes=pipeline_config.model.unet.filter_sizes)
    return functools.partial(_general_model_fn,
                             pipeline_config=pipeline_config,
                             result_folder=result_folder,
                             dataset_info=dataset_info,
                             feature_extractor=feature_extractor,
                             num_gpu=num_gpu,
                             visualization_file_names=visualization_file_names,
                             eval_dir=eval_dir)
  else:
    assert(False)
