import argparse
from distutils.util import strtobool
import numpy

from chainer_chemistry.datasets.citation_network.citation import citation_to_networkx  # NOQA
from chainer_chemistry.datasets.citation_network.citeseer import \
    get_citeseer_dirpath
from chainer_chemistry.datasets.citation_network.cora import get_cora_dirpath
from chainer_chemistry.datasets.reddit.reddit import reddit_to_networkx, \
    get_reddit_dirpath
from chainer_chemistry.dataset.networkx_preprocessors.base_networkx import BasePaddingNetworkxPreprocessor, BaseSparseNetworkxPreprocessor  # NOQA
from chainer_chemistry.dataset.graph_dataset.base_graph_data import PaddingGraphData  # NOQA
from chainer_chemistry.utils.train_utils import run_node_classification_train
from chainer_chemistry.models.prediction.node_classifier import NodeClassifier
from chainer_chemistry.models.gin import GINSparse, GIN
from chainer_chemistry.dataset.networkx_preprocessors.reddit_coo import get_reddit_coo_data  # NOQA

from padding_model_wrapper import PaddingModelWrapper  # NOQA


def get_cora():
    return citation_to_networkx(get_cora_dirpath(), "cora")


def get_citeseer():
    return citation_to_networkx(get_citeseer_dirpath(), "citeseer")


def get_reddit():
    return reddit_to_networkx(get_reddit_dirpath())


dataset_dict = {
    'cora': get_cora,
    'citeseer': get_citeseer,
    'reddit': get_reddit,
}
method_dict = {
    'gin': GIN,
    'gin_sparse': GINSparse,
}
preprocessor_dict = {
    'gin': BasePaddingNetworkxPreprocessor,
    'gin_sparse': BaseSparseNetworkxPreprocessor,
}


def parse_arguments():
    # Lists of supported preprocessing methods/models.
    dataset_list = ['cora', 'citeseer', 'reddit']
    method_list = ['gin', 'gin_sparse']

    # Set up the argument parser.
    parser = argparse.ArgumentParser(
        description='Node classification on network a graph')
    parser.add_argument('--dataset', type=str, choices=dataset_list,
                        default='cora', help='dataset name')
    parser.add_argument('--method', '-m', type=str, choices=method_list,
                        default='gin_sparse', help='method name')
    parser.add_argument('--conv-layers', '-c', type=int, default=2,
                        help='number of convolution layers')
    parser.add_argument(
        '--device', '-d', type=str, default='-1',
        help='Device specifier. Either ChainerX device specifier or an '
             'integer. If non-negative integer, CuPy arrays with specified '
             'device id are used. If negative integer, NumPy arrays are used')
    parser.add_argument('--out', '-o', type=str, default='result',
                        help='path to save the computed model to')
    parser.add_argument('--epoch', '-e', type=int, default=20,
                        help='number of epochs')
    parser.add_argument('--unit-num', '-u', type=int, default=32,
                        help='number of units in one layer of the model')
    parser.add_argument('--seed', '-s', type=int, default=777,
                        help='random seed value')
    parser.add_argument('--train-data-ratio', '-r', type=float, default=0.2,
                        help='ratio of training data w.r.t the dataset')
    parser.add_argument('--dropout', type=float, default=0.0,
                        help='dropout ratio')
    parser.add_argument('--coo', type=strtobool, default='false',
                        help='use Coo matrix')
    return parser.parse_args()


def generate_random_mask(n, train_num, seed=777):
    numpy.random.seed(seed)
    mask = numpy.zeros(n, dtype=bool)
    mask[:train_num] = True
    numpy.random.shuffle(mask)
    return mask, numpy.logical_not(mask)  # (train_mask, val_mask)


if __name__ == '__main__':
    args = parse_arguments()
    if args.dataset == 'reddit' and args.coo:
        # because it takes time to load reddit coo data via networkx
        data = get_reddit_coo_data(get_reddit_dirpath())
    else:
        networkx_graph = dataset_dict[args.dataset]()
        preprocessor = preprocessor_dict[args.method](use_coo=args.coo)
        data = preprocessor.construct_data(networkx_graph)
    print('label num: {}'.format(data.label_num))

    gnn = method_dict[args.method](out_dim=None, node_embedding=True,
                                   out_channels=data.label_num,
                                   hidden_channels=args.unit_num,
                                   n_update_layers=args.conv_layers,
                                   dropout_ratio=args.dropout)

    if isinstance(data, PaddingGraphData):
        gnn = PaddingModelWrapper(gnn)

    predictor = NodeClassifier(gnn, device=args.device)
    train_label_num = int(data.n_nodes * args.train_data_ratio)
    train_mask, valid_mask = generate_random_mask(
        data.n_nodes, train_label_num)
    print("train label: {}, validation label: {}".format(
        train_label_num, data.n_nodes - train_label_num))
    run_node_classification_train(
        predictor, data, train_mask, valid_mask,
        epoch=args.epoch, device=args.device)
