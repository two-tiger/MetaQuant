"""
Some meta networks
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import math

from utils.quantize import Function_STE, Function_BWN
from utils.miscellaneous import progress_bar
from utils.quantize import quantized_CNN, quantized_Linear
import utils.global_var as gVar
from mamba_ssm import Mamba

meta_count = 0

class TaLU(nn.Module):
    
    def __init__(self) -> None:
        super(TaLU, self).__init__()
        self.tanh = nn.Tanh()
        
    def forward(self, x):
        return x * self.tanh(x)

class MetaMamba(nn.Module):
    
    def __init__(self, d_model, d_state, d_conv, expand=4):
        super(MetaMamba, self).__init__()
        # self.pre_map = nn.Sequential(nn.Linear(d_model, d_model*expand), nn.Tanh(), nn.Linear(d_model*expand, d_model), nn.Tanh())
        self.mamba = Mamba(d_model=d_model, d_state=d_state, d_conv=d_conv, expand=expand)
        # self.mamba_out = Mamba(d_model=d_model, d_state=d_state, d_conv=d_conv, expand=expand)
        
    def forward(self, x, conv_state, ssm_state):
        
        x, conv_state, ssm_state = self.mamba.step(x, conv_state, ssm_state)
        
        # x = self.mamba_out(x)
        
        return x, conv_state, ssm_state
    
    
class MetaMambaHistory(nn.Module):
    
    def __init__(self, d_model, d_state, d_conv, expand=4):
        super(MetaMambaHistory, self).__init__()
        # self.pre_map = nn.Sequential(nn.Linear(d_model, d_model*expand), nn.Tanh(), nn.Linear(d_model*expand, d_model), nn.Tanh())
        self.mamba = Mamba(d_model=d_model, d_state=d_state, d_conv=d_conv, expand=expand)
        # self.mamba_out = Mamba(d_model=d_model, d_state=d_state, d_conv=d_conv, expand=expand)
        
    def forward(self, x):
        
        x = self.mamba(x)
        
        # x = self.mamba_out(x)
        
        return x
    

class MetaLSTMFC(nn.Module):

    def __init__(self, hidden_size = 20):
        super(MetaLSTMFC, self).__init__()

        self.hidden_size = hidden_size

        self.lstm1 = nn.LSTM(input_size=1, hidden_size = hidden_size, num_layers=1)
        self.fc1 = nn.Linear(in_features=hidden_size, out_features=1)

    def forward(self, x, hidden = None):

        if hidden is None:
            x, (hn1, cn1) = self.lstm1(x)
        else:
            x, (hn1, cn1) = self.lstm1(x, (hidden[0], hidden[1]))

        # x = self.fc1(x.view(-1, self.hidden_size))
        x = self.fc1(hn1.view(-1, self.hidden_size))

        return x, (hn1, cn1)


class MetaMultiLSTMFC(nn.Module):

    def __init__(self, hidden_size=20, num_lstm=2):
        super(MetaMultiLSTMFC, self).__init__()

        self.hidden_size = hidden_size

        self.lstm1 = nn.LSTM(input_size=1, hidden_size=hidden_size, num_layers=num_lstm)
        self.fc1 = nn.Linear(in_features=hidden_size, out_features=1)

    def forward(self, x, hidden=None):

        if hidden is None:
            x, (hn1, cn1) = self.lstm1(x)
        else:
            x, (hn1, cn1) = self.lstm1(x, (hidden[0], hidden[1]))

        x = self.fc1(x.view(-1, self.hidden_size))

        return x, (hn1, cn1)


class MetaFC(nn.Module):

    def __init__(self, hidden_size = 1500, symmetric_init=False, use_nonlinear=None):
        super(MetaFC, self).__init__()

        self.linear1 = nn.Linear(in_features=1, out_features=hidden_size, bias=False)
        self.linear2 = nn.Linear(in_features=hidden_size, out_features=1, bias=False)

        if symmetric_init:
            self.linear1.weight.data.fill_(1.0 / hidden_size)
            self.linear2.weight.data.fill_(1.0)

        self.use_nonlinear = use_nonlinear

    def forward(self, x):

        x = self.linear1(x)
        if self.use_nonlinear == 'relu':
            x = F.relu(x)
        elif self.use_nonlinear == 'tanh':
            x = torch.tanh(x)
        x = self.linear2(x)

        return x


class MetaMultiFC(nn.Module):

    def __init__(self, hidden_size = 10, use_nonlinear=None):
        super(MetaMultiFC, self).__init__()

        self.linear1 = nn.Linear(in_features=1, out_features=hidden_size, bias=False)
        self.linear2 = nn.Linear(in_features=hidden_size, out_features=hidden_size, bias=False)
        self.linear3 = nn.Linear(in_features=hidden_size, out_features=1, bias=False)

        self.use_nonlinear = use_nonlinear

    def forward(self, x):

        x = self.linear1(x)
        if self.use_nonlinear == 'relu':
            x = F.relu(x)
        elif self.use_nonlinear == 'tanh':
            x = torch.tanh(x)
        x = self.linear2(x)
        if self.use_nonlinear == 'relu':
            x = F.relu(x)
        elif self.use_nonlinear == 'tanh':
            x = torch.tanh(x)
        x = self.linear3(x)

        return x
    
    
class MetaLoRAMultiFC(nn.Module):
    
    def __init__(self, hidden_size = 10, rank = 4, use_nonlinear=None):
        super(MetaLoRAMultiFC, self).__init__()

        self.linear1 = nn.Linear(in_features=1, out_features=hidden_size, bias=False)
        self.linear2 = nn.Linear(in_features=hidden_size, out_features=hidden_size, bias=False)
        self.linear3 = nn.Linear(in_features=hidden_size, out_features=1, bias=False)

        self.use_nonlinear = use_nonlinear
        self.rank = rank

    def forward(self, x):

        x = self.linear1(x)
        if self.use_nonlinear == 'relu':
            x = F.relu(x)
        elif self.use_nonlinear == 'tanh':
            x = torch.tanh(x)
        x = self.linear2(x)
        if self.use_nonlinear == 'relu':
            x = F.relu(x)
        elif self.use_nonlinear == 'tanh':
            x = torch.tanh(x)
        x = self.linear3(x)

        return x


class MetaDesignedMultiFC(nn.Module):

    def __init__(self, hidden_size = 10, num_layers = 4, use_nonlinear='relu'):
        super(MetaDesignedMultiFC, self).__init__()

        self.use_nonlinear = use_nonlinear
        self.network = nn.Sequential()
        # self.linear = dict()
        for layer_idx in range(num_layers):

            in_features = 1 if layer_idx == 0 else hidden_size
            out_features = 1 if layer_idx == (num_layers-1) else hidden_size

            self.network.add_module('Linear%d' %layer_idx, nn.Linear(in_features=in_features, out_features=out_features, bias=False))

            if layer_idx != (num_layers-1):
                if self.use_nonlinear == 'relu':
                    self.network.add_module('ReLU%d' %layer_idx, nn.ReLU())
                elif self.use_nonlinear == 'tanh':
                    self.network.add_module('Tanh%d' %layer_idx, nn.Tanh())
                else:
                    # raise NotImplementedError
                    pass

    def forward(self, x):

        return self.network(x)


class MetaMultiFCBN(nn.Module):

    def __init__(self, hidden_size = 10, use_nonlinear = None):
        super(MetaMultiFCBN, self).__init__()

        self.linear1 = nn.Linear(in_features=1, out_features=hidden_size, bias=False)
        self.linear2 = nn.Linear(in_features=hidden_size, out_features=hidden_size, bias=False)
        self.linear3 = nn.Linear(in_features=hidden_size, out_features=1, bias=False)

        self.bn1 = nn.BatchNorm1d(num_features=hidden_size)
        self.bn2 = nn.BatchNorm1d(num_features=hidden_size)

        self.use_nonlinear = use_nonlinear

    def forward(self, x):

        x = self.linear1(x)
        x = self.bn1(x)
        if self.use_nonlinear == 'relu':
            x = F.relu(x)
        elif self.use_nonlinear == 'tanh':
            x = torch.tanh(x)
        x = self.linear2(x)
        x = self.bn2(x)
        if self.use_nonlinear == 'relu':
            x = F.relu(x)
        elif self.use_nonlinear == 'tanh':
            x = torch.tanh(x)
        x = self.linear3(x)

        return x


class MetaSimple(nn.Module):
    """
    A simple Meta model just multiplies a factor to the input gradient
    """
    def __init__(self):
        super(MetaSimple, self).__init__()

        self.alpha = nn.Parameter(torch.ones([1]))

    def forward(self, x):

        return self.alpha * x


class MetaCNN(nn.Module):
    def __init__(self):
        super(MetaCNN, self).__init__()
        self.conv1 = nn.Conv1d(in_channels=1, out_channels=16, kernel_size=3, stride=1, padding=1)
        self.conv2 = nn.Conv1d(in_channels=16, out_channels=32, kernel_size=3, stride=1, padding=1)
        self.fc = nn.Linear(32, 1)

    def forward(self, x):
        # 添加通道维度
        x = x.unsqueeze(1)

        # 应用卷积层和激活函数
        x = torch.relu(self.conv1(x))
        x = torch.relu(self.conv2(x))

        # 对特征图进行全局平均池化
        x = torch.mean(x, dim=2)

        # 应用全连接层
        x = self.fc(x)

        return x


class PositionalEncoding(nn.Module):
    def __init__(self, d_model, max_len=100):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=0.1)
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)

    def forward(self, x):
        seq_len = x.size(0)
        x = x + self.pe[:seq_len, :]
        return self.dropout(x)

class MetaTransformer(nn.Module):
    def __init__(self, d_model, nhead, num_layers):
        super(MetaTransformer, self).__init__()
        self.encoder = nn.TransformerEncoder(
            nn.TransformerEncoderLayer(d_model, nhead),
            num_layers
        )
        self.fc = nn.Linear(d_model, 1)

    def forward(self, x):
        seq_len = x.size(0)
        x = self.encoder(x)
        x = x.view(seq_len, -1)  # 将 x 的形状从 (seq_len, 1, d_model) 转换为 (seq_len, d_model)
        x = self.fc(x)
        x = x.view(seq_len, 1)  # 将 x 的形状从 (seq_len, 1) 转换回 (seq_len, 1)
        return x


class MetaLSTMLoRA(nn.Module):
    def __init__(self, hidden_size):
        super(MetaLSTMLoRA, self).__init__()
        self.hidden_size = hidden_size
        
        self.lstm = nn.LSTM(input_size=1, hidden_size=hidden_size)
        self.fc1 = nn.Linear(in_features=hidden_size, out_features=1)
        

    def forward(self, x, hidden):
        """
        输入梯度g，历史梯度（一阶动量）m
        输出LoRA分解后的向量，作为梯度矩阵
        """
        if hidden is None:
            x, (hn1, cn1) = self.lstm(x)
        else:
            x, (hn1, cn1) = self.lstm(x, (hidden[0], hidden[1]))

        x = self.fc1(x.view(-1, self.hidden_size))

        return x, (hn1, cn1)

def update_parameters(net, lr):
    for param in net.parameters():
        param.data.add_(-lr * param.grad.data)


def test(net, quantized_type, test_loader, use_cuda = True):

    net.eval()
    correct = 0
    total = 0
    for batch_idx, (inputs, targets) in enumerate(test_loader):
        if use_cuda:
            inputs, targets = inputs.cuda(), targets.cuda()

        outputs = net(inputs, quantized_type)

        _, predicted = torch.max(outputs.data, dim=1)
        correct += predicted.eq(targets.data).cpu().sum().item()
        total += targets.size(0)
        progress_bar(batch_idx, len(test_loader), "Test Acc: %.3f%%" % (100.0 * correct / total))

    return 100.0 * correct / total


if __name__ == '__main__':

    net = MetaDesignedMultiFC()

    torch.save(
        {
            'model': net,
            'hidden_size': 100,
            'nonlinear': 'None'
        }, './Results/meta_net.pkl'
    )

    meta_pack = torch.load('./Results/meta_net.pkl')

    retrieve_net = meta_pack['model']
    inputs = torch.rand([10, 1])
    outputs = retrieve_net(inputs)


