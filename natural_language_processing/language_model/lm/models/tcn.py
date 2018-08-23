from torch import nn
from torch.nn import functional as F
from torch.nn.utils import weight_norm

from .awd_lstm import VDEmbedding
from .dropout import VariationalDropout


class WDConv(nn.Module):
    """
    A variationally weight dropped ConvNet.
    To be used in the TCN convolutional block
    """

    def __init__(self, in_channels, out_channels, kernel_size, stride, dilation,
                 padding, dropout):
        super().__init__()

        # first, a conv layer
        self.conv = nn.Conv1d(in_channels, out_channels, kernel_size, stride, padding,
                               dilation)
        self.dropout = dropout

        self.init_weights()

        raw_weights = self.conv.weight
        del self.conv._parameters['weight']
        # assigning it to the module, so that we can call weight_norm on it
        self.register_parameter('raw_weight', raw_weights)

    def init_weights(self):
        self.conv.weight.data.normal_(0, 0.01)

    def forward(self, x):
        raw_weights = self.raw_weight
        weight_mask = nn.functional.dropout(raw_weights, p=self.dropout, training=self.training)

        # in case we're on a GPU
        if raw_weights.is_cuda: weight_mask = weight_mask.cuda()

        self.conv.register_parameter('weight', nn.Parameter(weight_mask))
        # this makes sure we only look into the past
        return self.conv(x)


class TCNBlock(nn.Module):
    """
    A weight dropped residual TCN Block
    """

    def __init__(self, in_channels, hidden_channels, kernel_size, stride, dilation, dropout):
        super().__init__()

        self.padding = (kernel_size - 1) * dilation

        self.conv1 = weight_norm(WDConv(in_channels, hidden_channels, kernel_size, stride=stride,
                                        padding=self.padding, dilation=dilation, dropout=dropout),
                                 name='raw_weight')
        self.conv2 = weight_norm(WDConv(hidden_channels, in_channels, kernel_size, stride=stride,
                                        padding=self.padding, dilation=dilation, dropout=dropout),
                                 name='raw_weight')

    def forward(self, x):
        out = F.relu(self.conv1(x)[:, :, :-self.padding])
        out = F.relu(self.conv2(out)[:, :, :-self.padding])
        return F.relu(out + x)


class ConvLM(nn.Module):
    """
    Arguments:
        embedding_dim: the input embedding size.
        hidden_channels: the output embedding sizes of the interim layers. Similar to hidden
            size in the awd_lstm
        kernel_size: float. Kernel sizes for the convolutional layers in the block
        conv_dropout: float. DropConnect values for the convolutional layers in the block.
        embedding_dropout: float. Dropout for the embedding layers
    """

    def __init__(self, num_blocks=2, embedding_dim=400, hidden_channels=1150, kernel_size=2, conv_dropout=0.2,
                 embedding_dropout=0.4, var_dropout_emb=0.1, vocab_size=30002, padding_idx=0):
        super().__init__()

        self.embedding = VDEmbedding(embedding_dropout, embedding_dim, vocab_size, padding_idx)
        self.num_blocks = num_blocks
        for block in range(num_blocks):
            dilation_size = 2 ** block
            convblock = TCNBlock(embedding_dim, hidden_channels, kernel_size, stride=1,
                                 dilation=dilation_size, dropout=conv_dropout)
            setattr(self, 'TCNBlock_{}'.format(block), convblock)

        self.decoder = nn.Linear(embedding_dim, vocab_size)

        # finally, dropouts
        self.emb_drop = VariationalDropout(p=var_dropout_emb)

    def init_weights(self):
        # tie weights
        self.decoder.weight = self.embedding.embedding.raw_weight
        self.decoder.bias.data.fill_(0)

    def forward(self, x):

        x = self.emb_drop(self.embedding(x))
        x = x.permute(0, 2, 1).contiguous()
        for block in range(self.num_blocks):
            x = getattr(self, 'TCNBlock_{}'.format(block))(x)
        x = x.permute(0, 2, 1).contiguous()
        # get the last output only
        x = x[:, -1, :].squeeze(1)
        return self.decoder(x)