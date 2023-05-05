import numpy as np
import pandas as pd
import torch
import random
import torch.nn.functional as F
from torch_geometric.data import Data
from torch_geometric.utils import degree
from torch_geometric.nn import GCNConv
from sklearn.metrics import classification_report

seed = 101
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
if torch.cuda.is_available():
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

class MaskEncoder(torch.nn.Module):
    def __init__(self, in_dim, h_dim, out_dim=16):
        super(MaskEncoder, self).__init__()
        self.convM1 = GCNConv(in_dim, h_dim)
        self.convM2 = GCNConv(h_dim, out_dim)

    def forward(self, x, edge_index):
        xM1 = F.relu(self.convB1(x, edge_index))
        xM1 = F.dropout(xM1, p=0.5, training=self.training)
        xM2 = self.convB2(xM1, edge_index)

        value = torch.sigmoid((xM2[edge_index[0]] * xM2[edge_index[1]]).sum(dim=1))
        _, topk_homo = torch.topk(value, int(len(value)*0.5), largest=True)
        _, topk_hetero = torch.topk(value, int(len(value)*0.5), largest=False)
        return edge_index[topk_homo], edge_index[topk_hetero]

class GCN_bot(torch.nn.Module):
    def __init__(self, in_dim, h_dim1, h_dim2, out_dim1=1, out_dim2=1):
        super(GCN_bot, self).__init__()
        self.convB1 = GCNConv(in_dim, h_dim1)
        self.convB2 = GCNConv(h_dim1, out_dim1)

        self.convY1 = GCNConv(in_dim, h_dim2)
        self.convY2 = GCNConv(h_dim2, out_dim2)

    def forward(self, x, edge_index):
        xB1 = F.relu(self.convB1(x, edge_index))
        xB1 = F.dropout(xB1, p=0.5, training=self.training)
        xB2 = self.convB2(xB1, edge_index)

        xY1 = F.relu(self.convY1(x, edge_index))
        xY1 = F.dropout(xY1, p=0.5, training=self.training)
        xY2 = self.convY2(xY1, edge_index)

        return xB2, xY2

def anomaly_loss(target, pred_score, m=1):
    # target: 0-1, pred_score: float
    Rs = torch.mean(pred_score)
    delta = torch.std(pred_score)
    dev_score = (pred_score - Rs)/delta
    cont_score = torch.max(torch.zeros(pred_score.shape), m-dev_score)
    loss = dev_score[(1-target).nonzero()].sum()+cont_score[target.nonzero()].sum()
    return loss

def evaluate_metric(pred_0, pred_1, pred_c0, pred_c1):
    tau_pred = torch.cat([pred_1, pred_c1], dim=0) - torch.cat([pred_0, pred_c0], dim=0)
    tau_true = torch.ones(tau_pred.shape)
    ePEHE = torch.sqrt(torch.mean(torch.square(tau_pred-tau_true)))
    eATE = torch.abs(torch.mean(tau_pred) - torch.mean(tau_true))
    return eATE, ePEHE

edge_index = torch.LongTensor(np.load('Dataset/synthetic/edge.npy'))
bot_label = np.load('Dataset/synthetic/bot_label.npy')
T = np.load('Dataset/synthetic/T_label.npy')
outcome = np.load('Dataset/synthetic/y.npy')

N = len(outcome)    # num of nodes
x = degree(edge_index[:,0])   # user node degree as feature
node_idx = [i for i in range(N)]
random.shuffle(node_idx)

train_mask = torch.zeros(N, dtype=torch.bool)
train_mask[node_idx[:int(N*0.8)]] = 1  # 前80%训练
test_mask = ~train_mask
target_var = torch.LongTensor(np.concatenate([bot_label[:,np.newaxis], outcome[:,np.newaxis], T[:,np.newaxis]], axis=-1))
botData = Data(x=x.unsqueeze(-1), edge_index=edge_index.t().contiguous(), y=target_var, train_mask=train_mask, test_mask=test_mask)

model1 = GCN_bot(in_dim=1, h_dim1=16, h_dim2=16, out_dim1=1, out_dim2=1)
optimizer = torch.optim.Adam(model1.parameters(), lr=0.01)

def train():
    for epoch in range(100):
        model1.train()
        optimizer.zero_grad()
        out_b, out_y = model1(botData.x, botData.edge_index)
        target_b, target_y = botData.y[:,0][botData.train_mask], botData.y[:,1][botData.train_mask]

        loss_b = anomaly_loss(target_b, out_b[botData.train_mask])
        loss_y = anomaly_loss(target_y, out_y[botData.train_mask])

        loss = loss_b + loss_y
        loss.backward()
        optimizer.step()

        if epoch%10 == 0:
            model1.eval()
            out_b, out_y = model1(botData.x, botData.edge_index)
            target_b, target_y = botData.y[:, 0][botData.train_mask], botData.y[:, 1][botData.train_mask]
            tc_label = botData.y[:, 2][botData.train_mask]

            # bot detection result
            threshold = torch.quantile(out_b, 0.96, dim=None, keepdim=False, interpolation='higher')
            pred_b = out_b.clone()
            pred_b[torch.where(out_b < threshold)] = 0
            pred_b[torch.where(out_b >= threshold)] = 1
            pred_label_b = pred_b[botData.train_mask]
            # outcome prediction result
            threshold = torch.quantile(out_y, 0.83, dim=None, keepdim=False, interpolation='higher')
            pred_y = out_y.clone()
            pred_y[torch.where(out_y < threshold)] = 0
            pred_y[torch.where(out_y >= threshold)] = 1
            pred_label_y = pred_y[botData.train_mask]
            # treatment effect prediction result
            idx_t, idx_c = torch.where(tc_label==1)[0], torch.where(tc_label==0)[0]
            true_t, true_c = target_y[idx_t], target_y[idx_c]
            pred_t, pred_c = pred_label_y[idx_t], pred_label_y[idx_c]
            # TODO: generate counterfactual outcome
            #eATE_test, ePEHE_test = evaluate_metric(pred_0, pred_1, pred_c0, pred_c1)


            report_b = classification_report(target_b, pred_label_b.detach().numpy())
            report_y = classification_report(target_y, pred_label_y.detach().numpy())
            print("Epoch " + str(epoch) + " bot-detect report:", report_b)
            print("Epoch "+str(epoch)+" outcome report:", report_y)



@torch.no_grad()
def test():
    model1.eval()
    out_b, out_y = model1(botData.x, botData.edge_index)

    # bot detection
    threshold = torch.quantile(out_b, 0.96, dim=None, keepdim=False, interpolation='higher')
    pred_b = out_b.clone()
    pred_b[torch.where(out_b < threshold)] = 0
    pred_b[torch.where(out_b >= threshold)] = 1
    pred_label_b = pred_b[botData.test_mask]
    target_b = botData.y[:,0][botData.test_mask]

    threshold = torch.quantile(out_y, 0.83, dim=None, keepdim=False, interpolation='higher')
    pred_y = out_y.clone()
    pred_y[torch.where(out_y < threshold)] = 0
    pred_y[torch.where(out_y >= threshold)] = 1
    pred_label_y = pred_y[botData.train_mask]
    target_y = botData.y[:, 1][botData.train_mask]

    report_b = classification_report(target_b, pred_label_b)
    report_y = classification_report(target_y, pred_label_y)
    print("bot-detect report:", report_b)
    print("outcome report:", report_y)


    # purchase prediction


train()
test()
