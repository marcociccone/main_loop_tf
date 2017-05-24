from copy import deepcopy
import hashlib
import logging
import os
import sys
from time import time

import copy
import dataset_loaders
import numpy as np
import tensorflow as tf
from tensorflow import nn
from tensorflow.contrib import slim
from tensorflow.python.framework import ops
from tensorflow.python.training import training
from tensorflow.python.training.supervisor import Supervisor
from tqdm import tqdm

import gflags
import loss
from utils import (apply_loss, split_in_gpu_chunks, save_repos_hash,
                   average_gradients, process_gradients, TqdmHandler)
from loss import mean_iou as compute_mean_iou

# config module load all flags from source files
import config  # noqa

import cv2
try:
    import pygtk  # noqa
    import gtk
    gtk.gdk.threads_init()
except:
    import warnings
    warnings.warn('pygtk is not installed, it will not be possible to '
                  'debug the optical flow')
    pygtk = None
    gtk = None

FLAGS = gflags.FLAGS
gflags.DEFINE_bool('help', False, 'If True, shows this message')
gflags.DEFINE_bool('debug', False, 'If True, enable tensorflow debug')
gflags.DEFINE_bool('return_extended_sequences', False, 'If True, repeats '
                   'the first and last frame of each video to allow for '
                   'middle frame prediction')
gflags.DEFINE_bool('return_middle_frame_only', False, 'If True, return '
                   'the middle frame segmentation mask only for each sequence')
gflags.DEFINE_string('model_name', 'my_model', 'The name of the model, '
                     'for the checkpoint file')
gflags.DEFINE_string('supervisor_master', '', 'The "master" string for the '
                     'Supervisor')


def run(argv, build_model):
    __parse_config(argv)
    # Run main with the remaining arguments
    __run(build_model)


def __parse_config(argv=None):
    gflags.mark_flags_as_required(['dataset'])

    # ============ Manage gflags
    # Parse FLAGS
    try:
        FLAGS(argv)  # parse flags
    except gflags.FlagsError as e:
        print('Usage: %s ARGS\n%s\n\nError: %s' % (argv[0], FLAGS, e))
        sys.exit(0)

    # Show help message
    if FLAGS.help:
        print('%s' % FLAGS)
        sys.exit(0)

    # Convert FLAGS to namespace, so we can modify it
    from argparse import Namespace
    cfg = Namespace()
    fl = FLAGS.FlagDict()
    cfg.__dict__ = {k: el.value for (k, el) in fl.iteritems()}
    gflags.cfg = cfg

    # ============ gsheet
    # Save params for log, excluding non JSONable and not interesting objects
    exclude_list = ['checkpoints_dir', 'checkpoints_to_keep', 'dataset',
                    'debug', 'debug_of', 'devices', 'do_validation_only',
                    'group_summaries', 'help', 'hyperparams_summaries',
                    'max_epochs', 'min_epochs', 'model_name', 'nthreads',
                    'patience', 'return_middle_frame_only', 'restore_model',
                    'save_gif_frames_on_disk', 'save_gif_on_disk',
                    'save_raw_predictions_on_disk', 'show_heatmaps_summaries',
                    'show_samples_summaries', 'supervisor_master',
                    'thresh_loss', 'train_summary_freq', 'use_threads',
                    'val_every_epochs', 'val_on_sets', 'val_skip_first',
                    'val_summary_freq', 'summary_per_subset']
    param_dict = {k: deepcopy(v) for (k, v) in cfg.__dict__.iteritems()
                  if k not in exclude_list}
    h = hashlib.md5()
    h.update(str(param_dict))
    cfg.hash = h.hexdigest()
    save_repos_hash(param_dict, cfg.model_name, ['tensorflow',
                                                 'dataset_loaders',
                                                 'main_loop_tf'])
    if cfg.restore_model is None or cfg.restore_model == 'False':
        # If you don't want to reload any model
        # Change the checkpoints directory if the model has not to be restored
        cfg.checkpoints_dir = os.path.join(cfg.checkpoints_dir, cfg.model_name,
                                           cfg.hash)
        incr_num = 0
        logdir = cfg.checkpoints_dir
        while(os.path.exists(logdir)):
            incr_num += 1
            if incr_num == 1:
                logdir += '_' + str(incr_num)
            else:
                logdir = logdir[:-2] + '_' + str(incr_num)
        cfg.checkpoints_dir = logdir
    else:
        restore_checkpoints_dir = os.path.join(cfg.checkpoints_dir,
                                               cfg.model_name,
                                               cfg.restore_model)
        # If you want to reload a specific  hash
        if os.path.exists(restore_checkpoints_dir):
            cfg.checkpoints_dir = restore_checkpoints_dir
        else:  # If you just want to reload the default hash
            cfg.checkpoints_dir = os.path.join(
                cfg.checkpoints_dir, cfg.model_name, cfg.hash)

    cfg.train_checkpoints_dir = os.path.join(cfg.checkpoints_dir, 'train')
    cfg.val_checkpoints_dir = os.path.join(cfg.checkpoints_dir, 'valid')

    # ============ A bunch of derived params
    cfg._FLOATX = 'float32'
    cfg.num_gpus = len([el for el in cfg.devices if 'gpu' in el])
    cfg.num_cpus = len([el for el in cfg.devices if 'cpu' in el])
    cfg.num_splits = cfg.num_gpus + cfg.num_cpus

    # Dataset
    try:
        Dataset = getattr(dataset_loaders, cfg.dataset)
    except AttributeError:
        Dataset = getattr(dataset_loaders, cfg.dataset.capitalize() +
                          'Dataset')
    cfg.Dataset = Dataset
    dataset_params = {}
    dataset_params['batch_size'] = cfg.batch_size
    dataset_params['data_augm_kwargs'] = {}
    dataset_params['data_augm_kwargs']['crop_size'] = cfg.crop_size
    dataset_params['data_augm_kwargs']['return_optical_flow'] = cfg.of
    dataset_params['return_one_hot'] = False
    dataset_params['return_01c'] = True
    if cfg.seq_per_subset:
        dataset_params['seq_per_subset'] = cfg.seq_per_subset
    if cfg.overlap is not None:
        dataset_params['overlap'] = cfg.overlap
    if cfg.seq_length:
        dataset_params['seq_length'] = cfg.seq_length

        cfg.input_shape = [None, cfg.seq_length] + list(Dataset.data_shape)
        cfg.val_input_shape = [None, cfg.seq_length] + list(Dataset.data_shape)

        if cfg.of:
            cfg.input_shape[-1] = 6
            cfg.val_input_shape[-1] = 6

        if cfg.crop_size:
            cfg.input_shape[2:4] = cfg.crop_size
        ret_ext_seq = cfg.return_extended_sequences
        ret_middle_frame = cfg.return_middle_frame_only
        dataset_params['return_extended_sequences'] = ret_ext_seq
        dataset_params['return_middle_frame_only'] = ret_middle_frame
    else:
        cfg.input_shape = [None] + list(Dataset.data_shape)
        cfg.val_input_shape = [None] + list(Dataset.data_shape)
        if cfg.crop_size:
            cfg.input_shape[1:3] = cfg.crop_size
    dataset_params['use_threads'] = cfg.use_threads
    dataset_params['nthreads'] = cfg.nthreads
    dataset_params['remove_per_img_mean'] = cfg.remove_per_img_mean
    dataset_params['divide_by_per_img_std'] = cfg.divide_by_per_img_std
    dataset_params['remove_mean'] = cfg.remove_mean
    dataset_params['divide_by_std'] = cfg.divide_by_std
    cfg.dataset_params = dataset_params
    cfg.valid_params = deepcopy(cfg.dataset_params)
    cfg.valid_params.update({
        'batch_size': cfg.val_batch_size,
        'seq_per_subset': 0,
        'overlap': cfg.val_overlap,
        'shuffle_at_each_epoch': (cfg.val_overlap is not None and
                                  cfg.val_overlap != 0),
        'return_middle_frame_only': False,
        'use_threads': False,  # prevent shuffling
        # prevent crop
        'data_augm_kwargs': {'return_optical_flow': cfg.of}})
    cfg.void_labels = getattr(Dataset, 'void_labels', [])
    cfg.nclasses = Dataset.non_void_nclasses
    cfg.nclasses_w_void = Dataset.nclasses
    tf.logging.info('{} classes ({} non-void):'.format(cfg.nclasses_w_void,
                                                       cfg.nclasses))

    # Optimization
    try:
        cfg.Optimizer = getattr(training, cfg.optimizer + 'Optimizer')
    except AttributeError:
        cfg.Optimizer = getattr(training, cfg.optimizer.capitalize() +
                                'Optimizer')
    try:
        loss_fn = getattr(nn, cfg.loss_fn)
    except AttributeError:
        try:
            loss_fn = getattr(nn, cfg.loss_fn.capitalize())
        except AttributeError:
            loss_fn = getattr(loss, cfg.loss_fn)
    cfg.loss_fn = loss_fn

    # TODO Add val_every_iter?
    cfg.val_skip = (cfg.val_skip_first if cfg.val_skip_first else
                    max(1, cfg.val_every_epochs) - 1)


def __run(build_model):
    cfg = gflags.cfg

    # ============ Class balance
    # assert class_balance in [None, 'median_freq_cost', 'rare_freq_cost'], (
    #     'The balance class method is not implemented')

    # if class_balance in ['median_freq_cost', 'rare_freq_cost']:
    #     if not hasattr(Dataset, 'class_freqs'):
    #         raise RuntimeError('class_freqs is missing for dataset '
    #                            '{}'.format(Dataset.name))
    #     freqs = Dataset.class_freqs

    #     if class_balance == 'median_freq_cost':
    #         w_freq = np.median(freqs) / freqs
    #     elif class_balance == 'rare_freq_cost':
    #         w_freq = 1 / (cfg.nclasses * freqs)

    #     tf.logging.info("Class balance weights", w_freq)
    #     cfg.class_balance = w_freq

    # ============ Train/validation
    # Load data
    # init_epoch = 0
    # prev_history = None
    # best_loss = np.Inf
    # best_val = np.Inf if early_stop_strategy == 'min' else -np.Inf
    # val_metrics_ext = ['val_' + m for m in val_metrics]
    # history_path = tmp_path + save_name + '.npy'
    # if cfg.reload_weights:
    #     # Reload weights
    #     pass

    # BUILD GRAPH
    tf_config = tf.ConfigProto(allow_soft_placement=True)

    tf.logging.info("Building the model ...")
    # with graph:
    with tf.Graph().as_default() as graph:
        cfg.global_step = tf.Variable(0, trainable=False, name='global_step',
                                      dtype='int32')

        # Create input placeholders for each gpu tower
        # All the placeholders are instantiated with a default value
        # since we want to be able to use a subset of them when the
        # batchsize is not big enough to fill all th GPUs and TF raises
        # a negative shape error if one placeholder is not fed (and has
        # no default value).
        # This is not a problem since when the default value is being
        # used we ignore it's output and do not perform gradient descent
        # on it.
        # Note: shape for labels placeholders will be None since we
        # provide flattened labels.
        inputs_per_gpu = []
        val_inputs_per_gpu = []
        labels_per_gpu = []
        for _ in range(cfg.num_splits):
            inputs_per_gpu.append(tf.placeholder_with_default(
                np.zeros(shape=[1]+cfg.input_shape[1:], dtype=cfg._FLOATX),
                shape=cfg.input_shape))
            val_inputs_per_gpu.append(tf.placeholder_with_default(
                np.zeros(shape=[1]+cfg.val_input_shape[1:], dtype=cfg._FLOATX),
                shape=cfg.val_input_shape))
            if cfg.seq_length:
                l_shape = np.prod(cfg.input_shape[2:])
            else:
                l_shape = np.prod(cfg.input_shape[1:])
            labels_per_gpu.append(tf.placeholder_with_default(
                np.zeros(shape=[l_shape], dtype=np.int32), shape=[None]))

        prev_err = tf.placeholder(shape=(),
                                  dtype=cfg._FLOATX, name='prev_err')
        if cfg.lr_decay is None:
            lr = cfg.lr
        elif cfg.lr_decay == 'exp':
            lr = tf.train.exponential_decay(cfg.lr,
                                            cfg.global_step,
                                            cfg.decay_steps,
                                            cfg.decay_rate,
                                            staircase=cfg.staircase)
        elif cfg.lr_decay == 'piecewise':
            lr = tf.train.piecewise_constant(cfg.global_step,
                                             cfg.lr_boundaries,
                                             cfg.lr_values)
        elif cfg.lr_decay == 'polynomial':
            lr = tf.train.polynomial_decay(cfg.lr,
                                           cfg.global_step,
                                           cfg.decay_steps,
                                           end_learning_rate=cfg.end_lr,
                                           power=cfg.power,
                                           cycle=cfg.staircase)

        elif cfg.lr_decay == 'natural_exp':
            lr = tf.train.natural_exp_decay(cfg.lr,
                                            cfg.global_step,
                                            cfg.decay_steps,
                                            cfg.decay_rate,
                                            staircase=cfg.staircase)
        elif cfg.lr_decay == 'inverse_time':
            lr = tf.train.inverse_time_decay(cfg.lr,
                                             cfg.global_step,
                                             cfg.decay_steps,
                                             cfg.decay_rate,
                                             staircase=cfg.staircase)
        else:
            raise NotImplementedError()

        cfg.Optimizer = cfg.Optimizer(learning_rate=lr, **cfg.optimizer_params)

        placeholders = [inputs_per_gpu, labels_per_gpu, prev_err]
        val_placeholders = [val_inputs_per_gpu, labels_per_gpu]

        # When the set size is not a multiple of the batch_size, the
        # last batch will be smaller than batch_size. When this happen
        # we might not be able to feed all the GPUs with an input for
        # this batch. Here we compute the number of GPUs to use for the
        # last batch of each set
        n_gpus_per_set = {}
        for s in ['train'] + cfg.val_on_sets:
            if s == 'train':
                params = cfg.dataset_params
            else:
                params = cfg.valid_params
            d_params = copy.deepcopy(params)
            d_params['batch_size'] *= cfg.num_splits
            this_set = cfg.Dataset(which_set=s, **d_params)
            batch_size = params['batch_size']
            last_batch_size = (this_set.nsamples -
                               (this_set.nbatches - 1) *
                               batch_size * cfg.num_splits)
            n_gpus_per_set[s] = last_batch_size // batch_size
            if last_batch_size % batch_size != 0:
                n_gpus_per_set[s] += 1

        # Model parameters on the FIRST device specified in cfg.devides
        # Gradient Average and the rest on the operations are on CPU
        with tf.device('/cpu:0'):
            # Model compilation
            # -----------------
            train_outs, train_summary_op, train_reset_cm_op = build_graph(
                placeholders, cfg.input_shape, build_model, True,
                n_gpus_per_set['train'], 'train')

            val_outs = {}
            val_summary_ops = {}
            val_reset_cm_op = {}
            for s in cfg.val_on_sets:
                (val_outs[s], val_summary_ops[s],
                 val_reset_cm_op[s]) = build_graph(
                     val_placeholders, cfg.val_input_shape,
                     build_model, False, n_gpus_per_set[s], s)
            if cfg.hyperparams_summaries is not None:
                sum_text = []
                for (key_header,
                     list_value) in cfg.hyperparams_summaries.iteritems():

                    header_list = []
                    text_list = []
                    for v in list_value:
                        header_list.append('**'+v+'**')
                        text_list.append(str(getattr(cfg, v)))
                    header_tensor = tf.constant(header_list)
                    text_tensor = tf.constant(text_list)

                    sum_text.append(tf.summary.text(
                        key_header, tf.reshape(
                            tf.concat([header_tensor, text_tensor], axis=0),
                            [2, -1])))
                sum_text_op = tf.summary.merge(sum_text)

            # Group global and local init into one op. Could be split into
            # two different ops and passed to `init_op` and `local_init_op`
            init_op = tf.group(tf.global_variables_initializer(),
                               tf.local_variables_initializer())
            saver = tf.train.Saver(max_to_keep=cfg.checkpoints_to_keep)

        sv = Supervisor(
            graph=graph,
            init_op=init_op,
            summary_op=None,
            global_step=cfg.global_step,
            logdir=cfg.checkpoints_dir,
            checkpoint_basename=cfg.model_name,
            saver=saver,
            # session_manager
            # summary_writer
            save_model_secs=300)
        cfg.sv = sv

        with sv.managed_session(cfg.supervisor_master, tf_config) as sess:
            cfg.sess = sess
            if cfg.debug:
                from tensorflow.python import debug as tf_debug
                sess = tf_debug.LocalCLIDebugWrapperSession(sess)
                sess.add_tensor_filter("has_inf_or_nan",
                                       tf_debug.has_inf_or_nan)

            if cfg.hyperparams_summaries is not None:
                # write Hyper parameters text summaries
                summary_str = cfg.sess.run(sum_text_op)
                sv.summary_computed(cfg.sess, summary_str)

            # Supervisor will always restore if a model is there.
            # TODO we probably need to move the checkpoints if restore
            # is not True?
            # if cfg.restore_model:
            #     # TODO add option to restore best rather than last?
            #     checkpoint = tf.train.latest_checkpoint(cfg.checkpoints_dir)
            #     tf.logging.info('Restoring model from checkpoint ' + checkpoint + '...')
            #     saver = tf.train.Saver()
            #     saver.restore(sess, checkpoint)
            #     tf.logging.info("Model restored.")

            if not cfg.do_validation_only:
                # Start training loop
                main_loop_kwags = {'placeholders': placeholders,
                                   'val_placeholders': val_placeholders,
                                   'train_outs': train_outs,
                                   'train_summary_op': train_summary_op,
                                   'val_outs': val_outs,
                                   'val_summary_ops': val_summary_ops,
                                   'val_reset_cm_op': val_reset_cm_op,
                                   'loss_fn': cfg.loss_fn,
                                   'Dataset': cfg.Dataset,
                                   'dataset_params': cfg.dataset_params,
                                   'valid_params': cfg.valid_params,
                                   'sv': sv,
                                   'saver': saver,
                                   's_gpus': n_gpus_per_set}
                return main_loop(**main_loop_kwags)
            else:
                # Perform validation only
                mean_iou = {}
                for s in cfg.val_on_sets:
                    from validate import validate
                    mean_iou[s] = validate(
                        val_placeholders,
                        val_outs[s],
                        val_summary_ops[s],
                        val_reset_cm_op[s],
                        n_gpus_per_set,
                        which_set=s)


def build_graph(placeholders, input_shape, build_model, is_training, s_gpus,
                which_set):
    ''' Build the multiGPU graph of computation

    This function creates a copy of the computation graph on each GPU. The
    result of the computation of each GPU is stored in a "tower"
    Note that thanks to the use of name_scopes and variable_scopes, calling
    this function multiple times does not create multiple copies of the *Ops*
    and of the *Variables* (respectively), but rather only adds the Ops that
    change from one call to the other and reuses the same Variables.

    Furthermore, to accommodate for the case where the last minibatch of a set
    is smaller than batch_size, we create two versions of each operation
    (e.g., those that compute the summaries), the first that uses all the GPUs
    (default) and the second that only uses the subset that is possible to use
    in the reduced batch case. Each returned value is then a list of two ops.
    This allows to choose at runtime which operations of the graph to call,
    depending on the batch size.
    '''
    cfg = gflags.cfg
    optimizer = cfg.Optimizer
    weight_decay = cfg.weight_decay
    loss_fn = cfg.loss_fn
    devices = cfg.devices
    nclasses = cfg.nclasses
    global_step = cfg.global_step
    if is_training:
        [inputs_per_gpu, labels_per_gpu, prev_err] = placeholders
    else:
        [inputs_per_gpu, labels_per_gpu] = placeholders

    # Init variables
    tower_grads = []
    tower_preds = []
    tower_soft_preds = []
    tower_losses = []
    # summaries = {}
    if is_training:
        summaries = tf.get_collection_ref(key='train_summaries')
    else:
        summaries = tf.get_collection_ref(
            key='val_' + which_set + '_summaries')
    tower_suffix = 'train' if is_training else 'val'

    # inputs_per_gpu, labels_per_gpu are lists
    for dev_idx, (dev_inputs, dev_labels) in enumerate(zip(inputs_per_gpu,
                                                           labels_per_gpu)):
        with tf.device(devices[dev_idx]):
            reuse_variables = not is_training or dev_idx > 0
            with tf.name_scope('GPU{}_{}'.format(dev_idx, tower_suffix)):
                with tf.variable_scope(cfg.model_name, reuse=reuse_variables):

                    net_out = build_model(dev_inputs, is_training)
                    softmax_pred = slim.softmax(net_out)
                    tower_soft_preds.append(softmax_pred)

                    # Prediction
                    pred = tf.argmax(softmax_pred, axis=-1)
                    tower_preds.append(pred)

                    # Loss
                    # Use softmax, unless using the
                    # tf.nn.sparse_softmax_cross_entropy function that
                    # internally applies it already
                    if (loss_fn is not
                            tf.nn.sparse_softmax_cross_entropy_with_logits):
                        net_out = softmax_pred
                    loss = apply_loss(dev_labels, net_out, loss_fn,
                                      weight_decay, is_training,
                                      return_mean_loss=True)
                    tower_losses.append(loss)
                    # Save this GPU's loss summary
                    summaries.append(tf.summary.scalar('Loss', loss))

                    # Gradients
                    if is_training:

                        # 1) Compute gradients
                        grads = optimizer.compute_gradients(
                             loss, colocate_gradients_with_ops=True)

                        # 2) Process gradients, average them later

                        if cfg.grad_noise_decay is None:
                            grad_noise_scale = cfg.grad_noise_scale
                        elif cfg.grad_noise_decay == 'annealing':

                            """
                            Adds annealed gaussian noise to the gradients at
                            every time step, by decaying the variance at each
                            time step
                            g_t <- g_t + N(0, sigma_t^2)
                            sigma_t^2 = eta / (1 + t)^gamma

                            with eta selected from {0.01, 0.3, 1.0) and
                            gamma = 0.55
                            See: "Adding gradient noise improves learning
                            for very deep networks",
                            http://arxiv.org/pdf/1511.06807v1.pdf
                            """

                            eta = cfg.grad_noise_scale ** 0.5
                            gamma = 0.55 / 2
                            grad_noise_scale = eta * tf.pow(tf.cast(
                                cfg.global_step + 1, cfg._FLOATX), -gamma)

                            summaries.append(tf.summary.scalar(
                                "Tower%d_NoiseGrad" % dev_idx,
                                grad_noise_scale))

                        elif cfg.grad_noise_decay == 'neural_gpu':
                            eta = cfg.grad_noise_scale
                            gamma = 0.55
                            grad_noise_scale = eta * tf.sqrt(
                                prev_err * tf.pow(tf.cast(
                                    cfg.global_step + 1, cfg._FLOATX), -gamma))

                            summaries.append(tf.summary.scalar(
                                "Tower%d_NoiseGrad" % dev_idx,
                                grad_noise_scale))

                        else:
                            raise NotImplementedError()
                        grads = process_gradients(grads,
                                                  grad_noise_scale,
                                                  cfg.grad_multiplier,
                                                  cfg.max_grad_norm)

            if is_training:

                # Add histograms for variables, grads and grad norms.
                for gradient, variable in grads:
                    if isinstance(gradient, tf.IndexedSlices):
                        grad_values = gradient.values
                    else:
                        grad_values = gradient

                    if grad_values is not None:
                        var_name = variable.name.replace(":", "_")
                        var_name = var_name.replace(
                            cfg.model_name+"/", "")
                        if cfg.group_summaries and var_name.count('/') >= 2:
                            var_name = var_name.replace("/", "_", 1)
                        summaries.append(tf.summary.histogram(
                            "Tower%d_Gradients_%s" % (dev_idx, var_name),
                            grad_values))

                        summaries.append(tf.summary.scalar(
                            "Tower%d_GradientNorm_%s" % (dev_idx, var_name),
                            tf.global_norm([grad_values])))

                summaries.append(tf.summary.scalar(
                    "Tower%d_Global_norm/clipped_grad_norm" % dev_idx,
                    tf.global_norm(list(zip(*grads))[0])))

                # Save gradients for each gpu to be averaged out
                tower_grads.append(grads)

            # Print regularization
            for v in tf.get_collection(
                    tf.GraphKeys.REGULARIZATION_LOSSES):
                tf.logging.debug('Regularization losses:\n{}'.format(v))

    if s_gpus == cfg.num_splits:
        num_gpus = [cfg.num_splits]
    else:
        # List containing the number of gpus in the full and subset case
        # respectively at the first and second position
        num_gpus = [cfg.num_splits, s_gpus]

    (preds, labels, softmax_preds, mask, preds_flat,
     avg_tower_loss, grads_and_vars,
     train_op, m_iou, per_class_iou, cm_update_op,
     reset_cm_op) = ([None] * 2 for _ in range(12))
    update_ops = [[], []]
    for idx, n_gpus in enumerate(num_gpus):
        preds[idx] = tf.concat(tower_preds[:n_gpus], axis=0),
        # Convert from list of tensors to tensor, and average
        softmax_preds[idx] = tf.concat(tower_soft_preds[:n_gpus], axis=0)

        # Concatenate the per-gpu placeholders to get a placeholder for the
        # full list of gpus and one for the subset to be used for
        # the minibatch with less batches
        labels[idx] = tf.concat(labels_per_gpu[:n_gpus], axis=0)

        # Compute the mean IoU
        # TODO would it be better to use less precision here?
        mask[idx] = tf.ones_like(labels[idx])
        if len(cfg.void_labels):
            mask[idx] = tf.cast(tf.less_equal(labels[idx], nclasses), tf.int32)
        preds_flat[idx] = tf.reshape(preds[idx], [-1])

        (m_iou[idx], per_class_iou[idx],
         cm_update_op[idx], reset_cm_op[idx]) = compute_mean_iou(
            labels[idx], preds_flat[idx], nclasses, mask[idx])

        # Compute the average *per variable* across the towers
        avg_tower_loss[idx] = tf.reduce_mean(tower_losses[:n_gpus])

        if is_training:
            # Impose graph dependency so that update operations are computed
            # even if they're are not explicit in the outputs os session.run
            grads_and_vars[idx] = average_gradients(tower_grads[:n_gpus])
            for i in range(n_gpus):
                update_ops[idx] += tf.get_collection(tf.GraphKeys.UPDATE_OPS,
                                                     scope='GPU{}'.format(i))
            with tf.control_dependencies(update_ops[idx]):
                train_op[idx] = optimizer.apply_gradients(
                    grads_and_vars=grads_and_vars[idx],
                    global_step=global_step)

        # TODO: Averaged gradients visualisation
        # Add the histograms of the gradients
        # with tf.name_scope('grad_summaries'):
        #     for grad, var in grads_and_vars:
        #         if grad is not None:
        #             summaries['training'].append(
        #                 tf.summary.histogram(
        #                   var.op.name + '/gradients', grad))

    #############
    # SUMMARIES #
    #############

    # Variables Histograms (training)
    if is_training:
        # Add the histograms for trainable variables
        for var in tf.trainable_variables():
            var_name = var.op.name.replace(cfg.model_name+'/', "")
            if cfg.group_summaries and var_name.count('/') >= 2:
                var_name = var_name.replace("/", "_", 1)
            var_name = 'Variables_' + var_name
            summaries.append(tf.summary.histogram(var_name, var))

    # Trainining or Validation summaries
    with tf.name_scope('summaries_{}'.format(tower_suffix)):

        # Duplicate summaries: this allow to distiguish between the
        # avg_tower_loss in the case of full num of gpus and that in
        # the case of a subset of num of gpus
        list_summaries = [summaries, summaries[:]]
        train_summary_op = [[], []]
        val_summary_op = [[], []]
        for idx, n_gpus in enumerate(num_gpus):

            # Scalars
            # list_summaries[idx].append(tf.summary.scalar(
            #     'Mean_tower_loss_' + which_set, avg_tower_loss[idx]))
            # We do it more fine-grained in validation.py
            # s.append(tf.summary.scalar('Mean_IoU_' + k, m_iou))

            # During the training we want to save informations about the
            # gradients, the trainable variables and the activations.

            if is_training:
                # Merge only the training summaries of the gpus used.
                # This is done searching by name since the variables are
                # always preceded by 'Tower{0,1,2,..}' or 'GPU{0,1,2,..}'
                training_summaries = []
                for i in range(n_gpus):
                    for el in list_summaries[idx]:
                        if ('GPU%d' % i in el.name or
                                'Tower%d' % i in el.name):
                            training_summaries += [el]
                        # if (i == 0 and
                        #         'Mean_tower_loss_' + which_set in el.name):
                        #     training_summaries += [el]
                train_summary_op[idx] = tf.summary.merge(training_summaries)
            else:
                # Same reasoning done for the training summaries (see
                # above)
                val_summaries = []
                for i in range(n_gpus):
                    for el in list_summaries[idx]:
                        if ('GPU%d' % i in el.name or
                            'Tower%d' % i in el.name):
                            val_summaries += [el]
                        # if (i == 0 and
                        #         'Mean_tower_loss_' + which_set in el.name):
                        #     val_summaries += [el]
                val_summary_op[idx] = tf.summary.merge(val_summaries)

    if is_training:
        return ([[avg_tower_loss[0], train_op[0]],
                 [avg_tower_loss[1], train_op[1]]],
                train_summary_op, reset_cm_op)
    else:
        return ([[preds[0], softmax_preds[0], m_iou[0], per_class_iou[0],
                  avg_tower_loss[0], cm_update_op[0]],
                 [preds[1], softmax_preds[1], m_iou[1], per_class_iou[1],
                  avg_tower_loss[1], cm_update_op[1]]],
                val_summary_op, reset_cm_op)


def main_loop(placeholders, val_placeholders, train_outs, train_summary_op,
              val_outs, val_summary_ops, val_reset_cm_op, loss_fn, Dataset,
              dataset_params, valid_params, sv, saver, s_gpus):

    # Add TqdmHandler
    handler = TqdmHandler()
    handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT, None))
    logger = logging.getLogger('tensorflow')
    del(logger.handlers[0])  # Remove the default handler
    logger.addHandler(handler)

    cfg = gflags.cfg
    max_epochs = cfg.max_epochs

    dataset_params['batch_size'] *= cfg.num_splits
    tf.logging.info('\nTrain dataset params:\n{}\n'.format(dataset_params))
    tf.logging.info('Validation dataset params:\n{}\n\n'.format(valid_params))
    train = Dataset(
        which_set='train',
        return_list=False,
        **dataset_params)

    # Setup loop parameters
    cum_iter = sv.global_step.eval(cfg.sess)
    val_skip = cfg.val_skip
    patience_counter = 0
    estop = False
    last_epoch = False
    history_acc = np.array([]).tolist()

    # Start the training loop.
    start = time()
    tf.logging.info("Beginning main loop...")
    loss_value = 0

    if pygtk and cfg.debug_of:
        cv2.namedWindow("rgb-optflow")

    while not sv.should_stop():
        epoch_id = cum_iter // train.nbatches
        pbar = tqdm(total=train.nbatches,
                    bar_format='{n_fmt}/{total_fmt}{desc}'
                               '{percentage:3.0f}%|{bar}| '
                               '[{elapsed}<{remaining},'
                               '{rate_fmt}{postfix}]')

        for batch_id in range(train.nbatches):
            cum_iter = sv.global_step.eval(cfg.sess)
            iter_start = time()

            # inputs and labels
            minibatch = train.next()
            t_data_load = time() - iter_start
            x_batch, y_batch = minibatch['data'], minibatch['labels']
            # sh = inputs.shape  # do NOT provide a list of shapes
            if pygtk and cfg.debug_of:
                for x_b in x_batch:
                    for x_frame in x_b:
                        rgb_of_frame = np.concatenate(
                            [x_frame[..., :3], x_frame[..., 3:]],
                            axis=1).astype(np.float32)
                        rgb_of_frame = cv2.cvtColor(rgb_of_frame,
                                                    cv2.COLOR_RGB2BGR)
                        cv2.imshow("rgb-optflow", rgb_of_frame)
                        cv2.waitKey(100)
            # reset_states(model, sh)

            # Compute the actual number of gpus used
            # for the current batch
            if x_batch.shape[0] != dataset_params['batch_size']:
                this_n_gpus = s_gpus['train']
            else:
                this_n_gpus = cfg.num_splits

            x_batch_chunks, y_batch_chunks = split_in_gpu_chunks(x_batch,
                                                                 y_batch,
                                                                 this_n_gpus)

            # Create dictionary to feed the input placeholders
            # placeholders = [inputs, labels, which_set,
            #                 input_split_dim, labels_split_dim]

            # Do not add noise if loss is less than threshold
            # TODO: It should be IoU or any other metric, but in this
            # case our loss is Dice Coefficient so it's fine
            loss_value = -1.0 if loss_value < -cfg.thresh_loss else loss_value
            in_values = x_batch_chunks + y_batch_chunks + [loss_value]
            if x_batch.shape[0] != dataset_params['batch_size']:
                s_placeholders = (placeholders[0][:s_gpus['train']] +
                                  placeholders[1][:s_gpus['train']] +
                                  [placeholders[2]])
                feed_dict = {p: v for(p, v) in zip(s_placeholders,
                                                   in_values)}
            else:
                f_placeholders = (placeholders[0] + placeholders[1] +
                                  [placeholders[2]])
                feed_dict = {p: v for (p, v) in zip(f_placeholders, in_values)}

            # train_op does not return anything, but must be in the
            # outputs to update the gradient
            if cum_iter % cfg.train_summary_freq == 0:
                if x_batch.shape[0] != dataset_params['batch_size']:
                    t_outs = train_outs[1] + [train_summary_op[1]]
                else:
                    t_outs = train_outs[0] + [train_summary_op[0]]
                loss_value, _, summary_str = cfg.sess.run(
                    t_outs, feed_dict=feed_dict)
                sv.summary_computed(cfg.sess, summary_str)
                # Write Mean_tower_loss summary
                summary = tf.Summary.Value(tag='Mean_tower_loss_train',
                                           simple_value=loss_value)
                summary_str = tf.Summary(value=[summary])
                sv.summary_computed(cfg.sess, summary_str)
            else:
                if x_batch.shape[0] != dataset_params['batch_size']:
                    t_outs = train_outs[1]
                else:
                    t_outs = train_outs[0]
                loss_value, _ = cfg.sess.run(t_outs, feed_dict=feed_dict)

            pbar.set_description('({:3d}) Ep {:d}'.format(cum_iter+1,
                                                          epoch_id+1))
            pbar.set_postfix({'D': '{:.2f}s'.format(t_data_load),
                              'loss': '{:.3f}'.format(loss_value)})
            pbar.update(1)

        # It's the end of the epoch
        pbar.close()
        # valid_wait = 0 if valid_wait == 1 else valid_wait - 1

        # Is it also the last epoch?
        if sv.should_stop() or epoch_id == max_epochs - 1:
            last_epoch = True

        # Early stop if patience is over
        patience_counter += 1
        if (epoch_id >= cfg.min_epochs and
                patience_counter >= cfg.patience):
            estop = True

        # TODO use tf.contrib.learn.monitors.ValidationMonitor?
        # Validate if last epoch, early stop or we reached valid_every
        if last_epoch or estop or not val_skip:
            # Validate
            mean_iou = {}
            from validate import validate
            for s in cfg.val_on_sets:
                mean_iou[s] = validate(
                    val_placeholders,
                    val_outs[s],
                    val_summary_ops[s],
                    val_reset_cm_op[s],
                    s_gpus,
                    which_set=s,
                    epoch_id=epoch_id)

            # TODO gsheet
            history_acc.append([mean_iou.get('valid')])

            # Did we improve *validation* mean IOU accuracy?
            best_hist = np.array(history_acc).max()
            if len(history_acc) == 0 or mean_iou.get('valid') >= best_hist:
                tf.logging.info('## Best model found! ##')
                t_save = time()
                checkpoint_path = os.path.join(cfg.checkpoints_dir,
                                               '{}_best.ckpt'.format(
                                                   cfg.model_name))

                saver.save(cfg.sess, checkpoint_path,
                           global_step=cfg.global_step)
                t_save = time() - t_save
                tf.logging.info('Checkpoint saved in {}s'.format(t_save))

                patience_counter = 0
                estop = False
            # Start skipping again
            val_skip = max(1, cfg.val_every_epochs) - 1
        else:
            # We skipped validation, decrease the counter
            val_skip -= 1

        # Verify epochs' loop exit conditions
        if estop:
            tf.logging.info('Early Stop!')
            sv.request_stop()
            break
        if last_epoch:
            tf.logging.info('Last epoch!')
            sv.request_stop()
            break

    max_valid_idx = np.argmax(np.array(history_acc))
    best = history_acc[max_valid_idx]
    (valid_mean_iou) = best

    tf.logging.info('\nBest: Mean Class iou - Valid {:.5f}\n'.format(
        valid_mean_iou))

    end = time()
    m, s = divmod(end - start, 60)
    h, m = divmod(m, 60)
    tf.logging.info("Total time elapsed: %d:%02d:%02d" % (h, m, s))

    # # Move complete models and stuff to shared fs
    # tf.logging.info('\n\nEND OF TRAINING!!\n\n')

    # def move_if_exist(filename, dest):
    #     if not os.path.exists(os.path.dirname(dest)):
    #         os.makedirs(os.path.dirname(dest))
    #     try:
    #         shutil.move(filename, dest)
    #     except IOError:
    #         tf.logging.error('Move error: {} does not exist.'.format(
    #             filename))

    # move_if_exist(tmp_path + save_name + "_best.w",
    #               'models/' + save_name + '_best.w')
    # move_if_exist(tmp_path + save_name + "_best_loss.w",
    #               'models/' + save_name + '_best_loss.w')
    # move_if_exist(tmp_path + save_name + "_latest.w",
    #               'models/' + save_name + '_latest.w')
    # move_if_exist(tmp_path + save_name + ".npy",
    #               'models/' + save_name + '.npy')
    # move_if_exist(tmp_path + save_name + ".svg",
    #               'models/' + save_name + '.svg')
    # validate = True  # Print the best model's test error
    return best
