#!/usr/bin/env python

from __future__ import print_function

import chainer
import numpy
import os
import pickle

from argparse import ArgumentParser
from chainer.datasets import split_dataset_random
from chainer import cuda
from chainer import functions as F
from chainer import optimizers
from chainer import training
from chainer import Variable
from chainer.iterators import SerialIterator
from chainer.training import extensions as E
from sklearn.preprocessing import StandardScaler

from chainer_chemistry.dataset.converters import concat_mols
from chainer_chemistry.dataset.parsers import CSVFileParser
from chainer_chemistry.dataset.preprocessors import preprocess_method_dict
from chainer_chemistry.datasets import NumpyTupleDataset
from chainer_chemistry.models import Regressor
from chainer_chemistry.models.prediction import set_up_predictor


class MeanAbsError(object):
    def __init__(self, scaler=None):
        """Initializes the (scaled) mean absolute error metric object.

        Args:
            scaler: Standard label scaler.
        """
        self.scaler = scaler

    def __call__(self, x0, x1):
        if isinstance(x0, Variable):
            x0 = cuda.to_cpu(x0.data)
        if isinstance(x1, Variable):
            x1 = cuda.to_cpu(x1.data)
        if self.scaler is not None:
            scaled_x0 = self.scaler.inverse_transform(cuda.to_cpu(x0))
            scaled_x1 = self.scaler.inverse_transform(cuda.to_cpu(x1))
            diff = scaled_x0 - scaled_x1
        else:
            diff = cuda.to_cpu(x0) - cuda.to_cpu(x1)
        return numpy.mean(numpy.absolute(diff), axis=0)[0]


class RootMeanSqrError(object):
    def __init__(self, scaler=None):
        """Initializes the (scaled) root mean square error metric object.

        Args:
            scaler: Standard label scaler.
        """
        self.scaler = scaler

    def __call__(self, x0, x1):
        if isinstance(x0, Variable):
            x0 = cuda.to_cpu(x0.data)
        if isinstance(x1, Variable):
            x1 = cuda.to_cpu(x1.data)
        if self.scaler is not None:
            scaled_x0 = self.scaler.inverse_transform(cuda.to_cpu(x0))
            scaled_x1 = self.scaler.inverse_transform(cuda.to_cpu(x1))
            diff = scaled_x0 - scaled_x1
        else:
            diff = cuda.to_cpu(x0) - cuda.to_cpu(x1)
        return numpy.sqrt(numpy.mean(numpy.power(diff, 2), axis=0)[0])


class ScaledAbsError(object):
    def __init__(self, scaler=None):
        """Initializes the (scaled) absolute error object.

        Args:
            scaler: Standard label scaler.
        """
        self.scaler = scaler

    def __call__(self, x0, x1):
        if isinstance(x0, Variable):
            x0 = cuda.to_cpu(x0.data)
        if isinstance(x1, Variable):
            x1 = cuda.to_cpu(x1.data)
        if self.scaler is not None:
            scaled_x0 = self.scaler.inverse_transform(cuda.to_cpu(x0))
            scaled_x1 = self.scaler.inverse_transform(cuda.to_cpu(x1))
            diff = scaled_x0 - scaled_x1
        else:
            diff = cuda.to_cpu(x0) - cuda.to_cpu(x1)
        return numpy.mean(numpy.absolute(diff), axis=0)[0]


def parse_arguments():
    # Lists of supported preprocessing methods/models.
    method_list = ['nfp', 'ggnn', 'schnet', 'weavenet', 'rsgcn', 'relgcn',
                   'relgat']
    scale_list = ['standardize', 'none']

    # Set up the argument parser.
    parser = ArgumentParser(description='Regression on own dataset')
    parser.add_argument('--datafile', '-d', type=str,
                        default='dataset_train.csv',
                        help='csv file containing the dataset')
    parser.add_argument('--method', '-m', type=str, choices=method_list,
                        help='method name', default='nfp')
    parser.add_argument('--label', '-l', nargs='+',
                        default=['value1', 'value2'],
                        help='target label for regression')
    parser.add_argument('--scale', type=str, choices=scale_list,
                        help='label scaling method', default='standardize')
    parser.add_argument('--conv-layers', '-c', type=int, default=4,
                        help='number of convolution layers')
    parser.add_argument('--batchsize', '-b', type=int, default=32,
                        help='batch size')
    parser.add_argument(
        '--device', type=str, default='-1',
        help='Device specifier. Either ChainerX device specifier or an '
             'integer. If non-negative integer, CuPy arrays with specified '
             'device id are used. If negative integer, NumPy arrays are used')
    parser.add_argument('--out', '-o', type=str, default='result',
                        help='path to save the computed model to')
    parser.add_argument('--epoch', '-e', type=int, default=10,
                        help='number of epochs')
    parser.add_argument('--unit-num', '-u', type=int, default=16,
                        help='number of units in one layer of the model')
    parser.add_argument('--seed', '-s', type=int, default=777,
                        help='random seed value')
    parser.add_argument('--train-data-ratio', '-r', type=float, default=0.7,
                        help='ratio of training data w.r.t the dataset')
    parser.add_argument('--protocol', type=int, default=2,
                        help='pickle protocol version')
    parser.add_argument('--model-filename', type=str, default='regressor.pkl',
                        help='saved model filename')
    return parser.parse_args()


def main():
    # Parse the arguments.
    args = parse_arguments()

    if args.label:
        labels = args.label
        class_num = len(labels) if isinstance(labels, list) else 1
    else:
        raise ValueError('No target label was specified.')

    # Dataset preparation. Postprocessing is required for the regression task.
    def postprocess_label(label_list):
        return numpy.asarray(label_list, dtype=numpy.float32)

    # Apply a preprocessor to the dataset.
    print('Preprocessing dataset...')
    preprocessor = preprocess_method_dict[args.method]()
    parser = CSVFileParser(preprocessor, postprocess_label=postprocess_label,
                           labels=labels, smiles_col='SMILES')
    dataset = parser.parse(args.datafile)['dataset']

    # Scale the label values, if necessary.
    if args.scale == 'standardize':
        scaler = StandardScaler()
        labels = scaler.fit_transform(dataset.get_datasets()[-1])
        dataset = NumpyTupleDataset(*(dataset.get_datasets()[:-1] + (labels,)))
    else:
        scaler = None

    # Split the dataset into training and validation.
    train_data_size = int(len(dataset) * args.train_data_ratio)
    train, _ = split_dataset_random(dataset, train_data_size, args.seed)

    # Set up the predictor.
    predictor = set_up_predictor(args.method, args.unit_num,
                                 args.conv_layers, class_num)

    # Set up the iterator.
    train_iter = SerialIterator(train, args.batchsize)

    # Set up the regressor.
    device = chainer.get_device(args.device)
    metrics_fun = {'mean_abs_error': MeanAbsError(scaler=scaler),
                   'root_mean_sqr_error': RootMeanSqrError(scaler=scaler)}
    regressor = Regressor(predictor, lossfun=F.mean_squared_error,
                          metrics_fun=metrics_fun, device=device)

    # Set up the optimizer.
    optimizer = optimizers.Adam()
    optimizer.setup(regressor)

    # Set up the updater.
    updater = training.StandardUpdater(train_iter, optimizer, device=device,
                                       converter=concat_mols)

    # Set up the trainer.
    print('Training...')
    trainer = training.Trainer(updater, (args.epoch, 'epoch'), out=args.out)
    trainer.extend(E.snapshot(), trigger=(args.epoch, 'epoch'))
    trainer.extend(E.LogReport())
    trainer.extend(E.PrintReport(['epoch', 'main/loss', 'main/mean_abs_error',
                                  'main/root_mean_sqr_error', 'elapsed_time']))
    trainer.extend(E.ProgressBar())
    try:
        trainer.run()
    except Exception as e:
        import IPython; IPython.embed()

    # Save the regressor's parameters.
    model_path = os.path.join(args.out, args.model_filename)
    print('Saving the trained model to {}...'.format(model_path))

    # TODO(nakago): ChainerX array cannot be sent to numpy array when internal
    # state has gradients.
    if hasattr(regressor.predictor.graph_conv, 'reset_state'):
        regressor.predictor.graph_conv.reset_state()

    regressor.save_pickle(model_path, protocol=args.protocol)

    # Save the standard scaler's parameters.
    if scaler is not None:
        with open(os.path.join(args.out, 'scaler.pkl'), mode='wb') as f:
            pickle.dump(scaler, f, protocol=args.protocol)


if __name__ == '__main__':
    main()
