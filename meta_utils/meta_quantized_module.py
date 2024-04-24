import torch
import torch.nn as nn
import torch.nn.functional as F

import math
import time

from utils.quantize import Function_STE, Function_BWN
from utils.miscellaneous import progress_bar, AverageMeter, accuracy
import utils.global_var as gVar


class MetaQuantConv(nn.Module):

    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1, bias=True, bitW = 2, layer_name=None):
        super(MetaQuantConv, self).__init__()

        gVar.meta_count += 1
        
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size

        self.weight = nn.Parameter(torch.Tensor(
                out_channels, in_channels // groups, kernel_size, kernel_size))
        if bias:
            self.bias = nn.Parameter(torch.Tensor(out_channels))
        else:
            self.bias = None

        self.stride = stride
        self.padding = padding
        self.dilation = dilation
        self.groups = groups
        self.meta_weight = None
        self.meta_bias = None # A temp variable for storing full-precision bias
        self.pre_quantized_weight = None
        self.quantized_weight = None
        self.bitW = bitW
        self.layer_name = layer_name
        self.n_elements = self.weight.numel()

        # Temp variable for storing gradients for meta weights and quantized weights for check mode
        self.meta_grad = None # Gradient generated by meta network
        self.quantized_grads = None # Gradient generated by loss function
        self.bias_grad = None
        self.pre_quantized_grads = None # Gradient generated by meta function using quantized_grads
        self.calibrated_grads = None # Gradient calibrated by pre-quantization function
        self.calibration = None # Calibration relationship from gradient of pre-quantized weights to origin weights
        self.refinement = None

        # Variable for BWN
        self.alpha = None

        n = math.sqrt(kernel_size * kernel_size * out_channels)
        self.weight.data.uniform_(0, math.sqrt(2. / n))
        if self.bias is not None:
            self.bias.data.uniform_(0, math.sqrt(2. / n))

        print('Initial Meta-Quantized CNN with bit %d' % self.bitW)

    def save_quantized_grad(self):
        def hook(grad):
            self.quantized_grads = grad
        return hook

    def save_pre_quantized_grad(self):
        def hook(grad):
            self.pre_quantized_grads = grad
        return hook
    
    def save_bias_grad(self):
        def hook(grad):
            self.bias_grad = grad
        return hook

    def forward(self, x, quantized_type = None, meta_grad = None, slow_grad = None, lr = 1e-3):

        # 校准
        if quantized_type == 'dorefa':
            self.calibration = 1 / (torch.max(torch.abs(torch.tanh(self.weight.data)))) \
                               * (1 - torch.pow(torch.tanh(self.weight.data), 2)).detach()
        elif quantized_type in ['BWN', 'BWN-F']:
            # alpha should be calculated in the previous iteration
            # self.calibration = torch.mean(torch.abs(self.weight.data))
            self.calibration = 1.0
        else:
            self.calibration = 1.0


        # Update meta weight
        if meta_grad is not None:

            # calibrated grads are gradients for original weights before any optimization acceleration technique
            self.calibrated_grads = meta_grad[1] * self.calibration
            # To incorprate meta network into original network's inference
            if slow_grad is None:
                self.meta_weight = self.weight - \
                                            lr * (self.calibrated_grads \
                                    + (self.weight.grad.data - self.calibrated_grads.data).detach())
            else:
                # 带momentum的SGD
                self.meta_weight = self.weight - lr * (0.9 * slow_grad[1] + (1 - 0.9) * self.calibrated_grads)
                
                # Adam 的更新规则
                # self.meta_weight = self.weight - lr * (self.calibrated_grads / (torch.sqrt(slow_grad[1] ** 2) + 1e-6))

        else:
            self.meta_weight = self.weight * 1.0

        # Update bias
        if self.bias is not None and meta_grad is not None:
            self.meta_bias = self.bias - self.bias.grad.data * lr
        elif self.bias is not None and meta_grad is None:
            self.meta_bias = self.bias * 1.0
        elif self.bias is None:
            pass
        else:
            raise Warning

        if quantized_type == 'dorefa':
            temp_weight = torch.tanh(self.meta_weight)
            self.pre_quantized_weight = (temp_weight / torch.max(torch.abs(temp_weight.data))) * 0.5 + 0.5 # 预处理函数
            self.quantized_weight = 2 * Function_STE.apply(self.pre_quantized_weight, self.bitW) - 1 # 量化函数
        elif quantized_type == 'BWN':
            self.alpha = torch.mean(torch.abs(self.meta_weight.data))
            self.pre_quantized_weight = self.meta_weight * 1.0
            self.quantized_weight = self.alpha.data * Function_BWN.apply(self.pre_quantized_weight)
        elif quantized_type == 'BWN-F':
            self.alpha = torch.abs(self.weight.data).mean(-1).mean(-1).mean(-1).view(-1, 1, 1, 1)
            self.pre_quantized_weight = self.meta_weight * 1.0
            self.quantized_weight = self.alpha.data * Function_BWN.apply(self.pre_quantized_weight)
        else:
            self.quantized_weight = self.meta_weight * 1.0

        try:
            self.quantized_weight.register_hook(self.save_quantized_grad())
            self.bias.register_hook(self.save_bias_grad())
            # 在反向传播的时候，将self.quantized_weight的梯度传给self.save_quantized_grad()方法
        except:
            pass

        return F.conv2d(x, self.quantized_weight, self.meta_bias, self.stride,
                        self.padding, self.dilation, self.groups)


class MetaQuantLinear(nn.Module):

    def __init__(self, in_features, out_features, bias=True, bitW = 2):
        super(MetaQuantLinear, self).__init__()

        gVar.meta_count += 1

        self.in_features = in_features
        self.out_features = out_features

        self.weight = nn.Parameter(torch.zeros([self.out_features, self.in_features]))
        if bias:
            self.bias = nn.Parameter(torch.rand([self.out_features]))
        else:
            self.bias = None

        self.meta_weight = None
        self.meta_bias = None
        self.pre_quantized_weight = None
        self.quantized_weight = None
        self.bitW = bitW
        self.n_elements = self.weight.numel()

        self.calibrated_grads = None

        # self.calibrated_grads = None
        self.quantized_grads = None
        self.pre_quantized_grads = None
        self.bias_grad = None
        self.calibration = None
        self.refinement = None

        # Variable for BWN
        self.alpha = None

        n = math.sqrt(self.out_features * self.in_features)
        self.weight.data.uniform_(0, math.sqrt(2. / n))
        if self.bias is not None:
            self.bias.data.uniform_(0, math.sqrt(2. / n))

        print('Initial Meta-Quantized Linear with bit %d' % self.bitW)


    def save_pre_quantized_grad(self):
        def hook(grad):
            self.pre_quantized_grads = grad
        return hook


    def save_quantized_grad(self):
        def hook(grad):
            self.quantized_grads = grad
        return hook
    
    def save_bias_grad(self):
        def hook(grad):
            self.bias_grad = grad
        return hook


    def forward(self, x, quantized_type = None, meta_grad = None, slow_grad = None, lr=1e-3):

        if quantized_type == 'dorefa':
            self.calibration = 1.0 / (torch.max(torch.abs(torch.tanh(self.weight.data))).detach()) \
                               * (1 - torch.pow(torch.tanh(self.weight.data), 2)).detach()
        elif quantized_type in ['BWN', 'BWN-F']:
            self.calibration = 1.0
        else:
            self.calibration = 1.0

        if meta_grad is not None:

            self.calibrated_grads = meta_grad[1] * self.calibration

            if slow_grad is None:
                self.meta_weight = self.weight - \
                                            lr * (self.calibrated_grads \
                                    + (self.weight.grad.data - self.calibrated_grads.data).detach())
            else:
                self.meta_weight = self.weight - lr * (0.9 * slow_grad[1] + (1 - 0.9) * self.calibrated_grads)
                
                # Adam 的更新规则
                # self.meta_weight = self.weight - lr * (self.calibrated_grads / (torch.sqrt(slow_grad[1] ** 2) + 1e-6))

        else:
            self.meta_weight = self.weight * 1.0

        # Update bias
        if self.bias is not None and meta_grad is not None:
            self.meta_bias = self.bias - self.bias.grad.data * lr
        elif self.bias is not None and meta_grad is None:
            self.meta_bias = self.bias * 1.0
        elif self.bias is None:
            pass
        else:
            raise Warning

        if quantized_type == 'dorefa':
            temp_weight = torch.tanh(self.meta_weight)
            self.pre_quantized_weight = (temp_weight / torch.max(torch.abs(temp_weight)).detach()) * 0.5 + 0.5
            self.quantized_weight = 2 * Function_STE.apply(self.pre_quantized_weight, self.bitW) - 1
            # print('The number of quantized weights 1: ', (self.quantized_weight == 1).sum().item())
        elif quantized_type in ['BWN', 'BWN-F']:
            # self.alpha = torch.sum(torch.abs(self.meta_weight.data)) / self.n_elements
            self.alpha = torch.mean(torch.abs(self.meta_weight.data))
            self.pre_quantized_weight = self.meta_weight * 1.0
            # self.alpha = torch.mean(torch.abs(self.pre_quantized_weight.data))
            self.quantized_weight = self.alpha * Function_BWN.apply(self.pre_quantized_weight)
        else:
            self.quantized_weight = self.meta_weight * 1.0

        try:
            self.quantized_weight.register_hook(self.save_quantized_grad())
            self.bias.register_hook(self.save_bias_grad())
        except:
            pass

        return F.linear(x, self.quantized_weight, self.meta_bias)


class MetaQuantConvWithLoRA(MetaQuantConv):
    
    @classmethod
    def from_object(cls, in_channels, out_channels, kernel_size, stride=1, padding=0, dilation=1, groups=1, bias=True, bitW=2, layer_name=None, rank=8, alpha_lora=16, in_obj:MetaQuantConv=None):
        out_obj = cls(in_channels, out_channels, kernel_size, stride, padding, dilation, groups, bias, bitW, layer_name, rank, alpha_lora)
        out_obj.weight = in_obj.weight
        if in_obj.bias is not None:
            out_obj.bias = in_obj.bias
        else:
            out_obj.bias = None
        return out_obj
    
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding=0, dilation=1, groups=1, bias=True, bitW=2, layer_name=None, rank=8, alpha_lora=16):
        super().__init__(in_channels, out_channels, kernel_size, stride, padding, dilation, groups, bias, bitW, layer_name)
        std_dev = 1 / torch.sqrt(torch.tensor(rank).float())
        self.A = torch.nn.Parameter(torch.randn(self.out_channels*self.kernel_size, rank) * std_dev)
        self.B = torch.nn.Parameter(torch.randn(rank, self.in_channels*self.kernel_size))
        self.alpha_lora = alpha_lora
        
        self.A_grad = None
        self.B_grad = None
        self.calibrated_grads_A = None
        self.calibrated_grads_B = None
        self.meta_A = None
        self.meta_B = None
        self.delta_w = None
        self.merge_w = None

    def save_A_grad(self):
        def hook(grad):
            self.A_grad = grad
        return hook
    
    def save_B_grad(self):
        def hook(grad):
            self.B_grad = grad
        return hook
        
    def forward(self, x, quantized_type = None, meta_grad = None, slow_grad = None, lr = 1e-3):
        # 校准
        if quantized_type == 'dorefa':
            self.calibration = 1 / (torch.max(torch.abs(torch.tanh(self.weight.data)))) \
                               * (1 - torch.pow(torch.tanh(self.weight.data), 2)).detach()
            self.calibration_A = 1 / (torch.max(torch.abs(torch.tanh(self.A.data)))) \
                               * (1 - torch.pow(torch.tanh(self.A.data), 2)).detach()
            self.calibration_B = 1 / (torch.max(torch.abs(torch.tanh(self.B.data)))) \
                               * (1 - torch.pow(torch.tanh(self.B.data), 2)).detach()
        elif quantized_type in ['BWN', 'BWN-F']:
            # alpha should be calculated in the previous iteration
            # self.calibration = torch.mean(torch.abs(self.weight.data))
            self.calibration = 1.0
            self.calibration_A = 1.0
            self.calibration_B = 1.0
        else:
            self.calibration = 1.0
            self.calibration_A = 1.0
            self.calibration_B = 1.0


        # Update meta weight
        if meta_grad is not None:

            # calibrated grads are gradients for original weights before any optimization acceleration technique
            self.calibrated_grads_A = meta_grad[1][0] * self.calibration_A
            self.calibrated_grads_B = meta_grad[1][1] * self.calibration_B # fast pre
            # To incorprate meta network into original network's inference
            # 带momentum的SGD
            self.meta_A = torch.nn.Parameter(self.A - lr * (0.9 * slow_grad[1][0] + (1 - 0.9) * self.calibrated_grads_A)).cuda()
            self.meta_B = torch.nn.Parameter(self.B - lr * (0.9 * slow_grad[1][1] + (1 - 0.9) * self.calibrated_grads_B)).cuda()
            
            # Adam 的更新规则
            # self.meta_weight = self.weight - lr * (self.calibrated_grads / (torch.sqrt(slow_grad[1] ** 2) + 1e-6))

        else:
            self.meta_A = self.A
            self.meta_B = self.B
        
        self.delta_w = self.meta_A @ self.meta_B
        self.delta_w = self.delta_w.view(self.out_channels, self.in_channels // self.groups, self.kernel_size, self.kernel_size)
        self.merge_w = self.weight + self.delta_w

        # Update bias
        if self.bias is not None and meta_grad is not None:
            self.meta_bias = self.bias - meta_grad[2] * lr
        elif self.bias is not None and meta_grad is None:
            self.meta_bias = self.bias * 1.0
        elif self.bias is None:
            pass
        else:
            raise Warning

        if quantized_type == 'dorefa':
            temp_weight = torch.tanh(self.merge_w)
            self.pre_quantized_weight = (temp_weight / torch.max(torch.abs(temp_weight.data))) * 0.5 + 0.5 # 预处理函数
            self.quantized_weight = 2 * Function_STE.apply(self.pre_quantized_weight, self.bitW) - 1 # 量化函数
        elif quantized_type == 'BWN':
            self.alpha = torch.mean(torch.abs(self.meta_weight.data))
            self.pre_quantized_weight = self.meta_weight * 1.0
            self.quantized_weight = self.alpha.data * Function_BWN.apply(self.pre_quantized_weight)
        elif quantized_type == 'BWN-F':
            self.alpha = torch.abs(self.weight.data).mean(-1).mean(-1).mean(-1).view(-1, 1, 1, 1)
            self.pre_quantized_weight = self.meta_weight * 1.0
            self.quantized_weight = self.alpha.data * Function_BWN.apply(self.pre_quantized_weight)
        else:
            self.quantized_weight = self.meta_weight * 1.0

        try:
            # self.quantized_weight.register_hook(self.save_quantized_grad())
            self.A.register_hook(self.save_A_grad())
            self.B.register_hook(self.save_B_grad())
            # 在反向传播的时候，将self.quantized_weight的梯度传给self.save_quantized_grad()方法
        except:
            pass

        return F.conv2d(x, self.quantized_weight, self.meta_bias, self.stride,
                        self.padding, self.dilation, self.groups)


class MetaQuantLinearWithLoRA(MetaQuantLinear):
    
    @classmethod
    def from_object(cls, in_features, out_features, bias=True, bitW=2, rank=8, alpha_lora=16, in_obj:MetaQuantLinear=None):
        out_obj = cls(in_features, out_features, bias, bitW, rank, alpha_lora)
        out_obj.weight = in_obj.weight
        if in_obj.bias is not None:
            out_obj.bias = in_obj.bias
        else:
            out_obj.bias = None
        return out_obj
    
    def __init__(self, in_features, out_features, bias=True, bitW=2, rank=8, alpha_lora=16):
        super().__init__(in_features, out_features, bias, bitW)
        std_dev = 1 / torch.sqrt(torch.tensor(rank).float())
        self.A = torch.nn.Parameter(torch.randn(self.out_features, rank) * std_dev)
        self.B = torch.nn.Parameter(torch.zeros(rank, self.in_features))
        self.alpha_lora = alpha_lora
        
        self.A_grad = None
        self.B_grad = None
        self.calibrated_grads_A = None
        self.calibrated_grads_B = None
        self.meta_A = None
        self.meta_B = None
        self.delta_w = None
        self.merge_w = None
        
    def save_A_grad(self):
        def hook(grad):
            self.A_grad = grad
        return hook
    
    def save_B_grad(self):
        def hook(grad):
            self.B_grad = grad
        return hook
        
    def forward(self, x, quantized_type = None, meta_grad = None, slow_grad = None, lr=1e-3):
        if quantized_type == 'dorefa':
            self.calibration = 1 / (torch.max(torch.abs(torch.tanh(self.weight.data))).detach()) * (1 - torch.pow(torch.tanh(self.weight.data), 2)).detach()
            self.calibration_A = 1 / (torch.max(torch.abs(torch.tanh(self.A.data)))) \
                               * (1 - torch.pow(torch.tanh(self.A.data), 2)).detach()
            self.calibration_B = 1 / (torch.max(torch.abs(torch.tanh(self.B.data)))) \
                               * (1 - torch.pow(torch.tanh(self.B.data), 2)).detach()
        elif quantized_type in ['BWN', 'BWN-F']:
            self.calibration = 1.0
            self.calibration_A = 1.0
            self.calibration_B = 1.0
        else:
            self.calibration = 1.0
            self.calibration_A = 1.0
            self.calibration_B = 1.0

        if meta_grad is not None:

            self.calibrated_grads_A = meta_grad[1][0] * self.calibration_A
            self.calibrated_grads_B = meta_grad[1][1] * self.calibration_B

            # 带momentum的SGD
            self.meta_A = torch.nn.Parameter(self.A - lr * (0.9 * slow_grad[1][0] + (1 - 0.9) * self.calibrated_grads_A)).cuda()
            self.meta_B = torch.nn.Parameter(self.B - lr * (0.9 * slow_grad[1][1] + (1 - 0.9) * self.calibrated_grads_B)).cuda()
            
            # Adam 的更新规则
            # self.meta_weight = self.weight - lr * (self.calibrated_grads / (torch.sqrt(slow_grad[1] ** 2) + 1e-6))

        else:
            self.meta_A = self.A
            self.meta_B = self.B
            
        self.delta_w = self.meta_A @ self.meta_B
        self.merge_w = self.weight + self.delta_w

        # Update bias
        if self.bias is not None and meta_grad is not None:
            self.meta_bias = self.bias - meta_grad[2] * lr
        elif self.bias is not None and meta_grad is None:
            self.meta_bias = self.bias * 1.0
        elif self.bias is None:
            pass
        else:
            raise Warning

        if quantized_type == 'dorefa':
            temp_weight = torch.tanh(self.merge_w)
            self.pre_quantized_weight = (temp_weight / torch.max(torch.abs(temp_weight)).detach()) * 0.5 + 0.5
            self.quantized_weight = 2 * Function_STE.apply(self.pre_quantized_weight, self.bitW) - 1
            # print('The number of quantized weights 1: ', (self.quantized_weight == 1).sum().item())
        elif quantized_type in ['BWN', 'BWN-F']:
            # self.alpha = torch.sum(torch.abs(self.meta_weight.data)) / self.n_elements
            self.alpha = torch.mean(torch.abs(self.meta_weight.data))
            self.pre_quantized_weight = self.meta_weight * 1.0
            # self.alpha = torch.mean(torch.abs(self.pre_quantized_weight.data))
            self.quantized_weight = self.alpha * Function_BWN.apply(self.pre_quantized_weight)
        else:
            self.quantized_weight = self.meta_weight * 1.0

        try:
            # self.quantized_weight.register_hook(self.save_quantized_grad())
            self.meta_A.register_hook(self.save_A_grad())
            self.meta_B.register_hook(self.save_B_grad())
        except:
            pass

        return F.linear(x, self.quantized_weight, self.meta_bias)


def test(net, quantized, test_loader, use_cuda = True, dataset_name='CIFAR10', n_batches_used=None):

    net.eval()

    if dataset_name not in ['ImageNet']:
        correct = 0
        total = 0
        for batch_idx, (inputs, targets) in enumerate(test_loader):
            if use_cuda:
                inputs, targets = inputs.cuda(), targets.cuda()

            with torch.no_grad():
                outputs = net(inputs, quantized_type=quantized)

            _, predicted = torch.max(outputs.data, dim=1)
            correct += predicted.eq(targets.data).cpu().sum().item()
            total += targets.size(0)
            progress_bar(batch_idx, len(test_loader), "Test Acc: %.3f%%" % (100.0 * correct / total))

        return 100.0 * correct / total

    else:

        batch_time = AverageMeter()
        train_loss = AverageMeter()
        top1 = AverageMeter()
        top5 = AverageMeter()

        with torch.no_grad():
            end = time.time()
            for batch_idx, (inputs, targets) in enumerate(test_loader):
                if use_cuda:
                    inputs, targets = inputs.cuda(), targets.cuda()
                outputs = net(inputs, quantized_type=quantized)
                losses = nn.CrossEntropyLoss()(outputs, targets)

                prec1, prec5 = accuracy(outputs.data, targets.data, topk=(1, 5))
                train_loss.update(losses.data.item(), inputs.size(0))
                top1.update(prec1.item(), inputs.size(0))
                top5.update(prec5.item(), inputs.size(0))

                # measure elapsed time
                batch_time.update(time.time() - end)
                end = time.time()

                if batch_idx % 200 == 0:
                    print('Test: [{0}/{1}]\t'
                          'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                          'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                          'Acc@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                          'Acc@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                        batch_idx, len(test_loader), batch_time=batch_time, loss=train_loss,
                        top1=top1, top5=top5))

                if n_batches_used is not None and batch_idx >= n_batches_used:
                    break

        print(' * Acc@1 {top1.avg:.3f} Acc@5 {top5.avg:.3f}'
              .format(top1=top1, top5=top5))

        return top1.avg, top5.avg


class testNet(nn.Module):

    def __init__(self):
        super(testNet, self).__init__()
        self.conv1 = MetaQuantConv(in_channels=3, out_channels=32, kernel_size=3, stride=1, padding=1)

    def forward(self, x):

        return self.conv1(x)

if __name__ == '__main__':

    net = testNet()
    inputs = torch.rand([10, 3, 32, 32])
    targets = torch.rand([10, 32, 32, 32])

    outputs = net(inputs)
    losses = torch.nn.MSELoss()(outputs, targets)