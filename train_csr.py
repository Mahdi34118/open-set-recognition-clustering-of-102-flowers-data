import pickle
import numpy as np
import os

from torchvision import transforms
from torch.utils.data import DataLoader
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image
import timm
from tqdm import tqdm
import matplotlib.pyplot as plt
import json

rng = np.random.default_rng(seed=2023)

class ImageDataset:
  def __init__(self, data, labels, n_classes, ls_eps, transform):
    self.data = data
    self.labels = labels
    self.ls_eps = ls_eps
    self.n_classes = n_classes
    self.transform = transform

  def __len__(self):
    return len(self.data)
  
  def __getitem__(self, idx):
    if self.ls_eps > 0:
      label = self.label_smoothing(self.labels[idx])
    else:
      label = self.labels[idx]
    return self.transform(Image.fromarray(self.data[idx])), torch.tensor(label)

  def view(self, idx):
    return self.data[idx], self.label[idx]

  def label_smoothing(self, label):
    out = np.ones(self.n_classes) * self.ls_eps / (self.n_classes - 1)
    out[label] = 1 - self.ls_eps
    return out

class OpensetRecognizer(nn.Module):
  def __init__(self, data_path, feature_dim):
    super().__init__()
    self.data_path = data_path
    self.train_transform = transforms.Compose([
        transforms.RandAugment(num_ops=6, magnitude=10, num_magnitude_bins=51),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        ])
    self.val_transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    self._load_data()
    self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    self.feature_dim = feature_dim
    self._load_model(feature_dim)
    self.to(self.device)

  def train_collate(self, data):
    img, labels = zip(*data)
    img = torch.stack(img).float()
    labels = torch.stack(labels).float()
    return img, labels

  def val_collate(self, data):
    img, labels = zip(*data)
    img = torch.stack(img).float()
    labels = torch.stack(labels).long()
    return img, labels

  def _load_data(self):
    train_data = pickle.load(open(os.path.join(self.data_path, 'train_data.pickle'), 'rb'))
    train_labels = pickle.load(open(os.path.join(self.data_path, 'train_labels.pickle'), 'rb'))

    train_labels_set = sorted(list(set(train_labels)))
    train_labels_mapping = {train_labels_set[i]:i for i in range(len(train_labels_set))}

    train_data = np.stack(train_data)

    train_labels = np.array([train_labels_mapping[item] for item in train_labels])

    self.N = len(train_labels_set)
    val_inds = []
    train_inds = []
    for i in range(self.N):
      all_idx = rng.permutation(np.nonzero(train_labels == i)[0])
      val_idx = all_idx[:int(0.1 * len(all_idx))]
      train_idx = all_idx[int(0.1 * len(all_idx)):]
      train_inds.append(train_idx)
      val_inds.append(val_idx)

    val_inds = np.concatenate(val_inds)
    train_inds = np.concatenate(train_inds)

    val_data = train_data[val_inds]
    train_data = train_data[train_inds]
    val_labels = train_labels[val_inds]
    train_labels = train_labels[train_inds]

    train_dataset = ImageDataset(train_data, train_labels, n_classes=7, ls_eps=0.1, transform=self.train_transform)
    val_dataset = ImageDataset(val_data, val_labels, n_classes=7, ls_eps=0, transform=self.val_transform)

    train_loader = DataLoader(train_dataset, batch_size=16, shuffle=True, collate_fn=self.train_collate, drop_last=False)
    val_loader = DataLoader(val_dataset, batch_size=16, shuffle=False, collate_fn=self.val_collate, drop_last=False)

    self.loaders = {'train': train_loader, 'val': val_loader}

  def CEWithSmoothLabels(self, output, target):
    log_pred = F.log_softmax(output, dim=-1)
    return torch.mean(torch.sum(-log_pred * target, dim=-1))
  
  def _load_model(self, feature_dim):
    self.model = timm.create_model('efficientnet_b3', pretrained=True)
    self.model.classifier = nn.Sequential(
        nn.Linear(1536, feature_dim),
        nn.BatchNorm1d(feature_dim),
        nn.ReLU(),
        nn.Linear(feature_dim, self.N)
    )
    params_to_optim = []
    for n, p in self.model.named_parameters():
      if n.startswith('classifier') or n.startswith('bn2') or n.startswith('conv_head'):
        p.requires_grad = True
        params_to_optim.append(p)
      else:
        p.requires_grad = False
    self.model_params = params_to_optim

  def _prepare_training(self, n_epochs):
    self.optimizer = torch.optim.Adam(self.model_params, lr=1e-4, weight_decay=1e-4)
    self.scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer=self.optimizer, max_lr=1e-3, epochs=n_epochs, steps_per_epoch=len(self.loaders['train']))
    train_criterion = self.CEWithSmoothLabels
    val_criterion = nn.CrossEntropyLoss()
    self.criterions = {'train': train_criterion, 'val': val_criterion}

  def train_model(self, n_epochs):
    self._prepare_training(n_epochs)

    losses = {'train': [], 'val': []}
    accs = {'train': [], 'val': []}

    best_acc = 0.0
    for epoch in range(n_epochs):
      print(f"######## EPOCH {epoch + 1}/{n_epochs} started")
      for mode in ['train', 'val']:
        if mode == 'train':
          self.model.train()
        else:
          self.model.eval()
        total_loss = 0
        total_acc = 0
        count = 0
        for img, label in tqdm(self.loaders[mode]):
          img = img.to(self.device)
          label = label.to(self.device)
          bsz = len(label)
          with torch.set_grad_enabled(mode == 'train'):
            output = self.model(img)
          
          loss = self.criterions[mode](output, label)
          total_loss += loss.item() * bsz

          target = label if mode == 'val' else torch.argmax(label, dim=-1)
          pred = torch.argmax(output, dim=-1)
          
          total_acc += torch.sum(pred == target).item()

          count += bsz

          if mode == 'train':
            loss.backward()
            self.optimizer.step()
            self.optimizer.zero_grad()
            self.scheduler.step()

        losses[mode].append(total_loss / count)
        accs[mode].append(total_acc / count * 100)
        if mode == 'val':
          if accs[mode][-1] > best_acc:
            best_acc = accs[mode][-1]
            torch.save(self.model.state_dict(), f'model_{self.feature_dim}.pt')
            print(f"--> best model saved. ACC={best_acc:.2f}")
        print(f"{mode.upper()} done: loss = {losses[mode][-1]:.4f}, accuracy = {accs[mode][-1]:.2f}")
    self.model.load_state_dict(torch.load(f'model_{self.feature_dim}.pt'))
    return losses, accs

  def prepare_detection_model(self):
    mls_closed = []
    self.model.eval()
    total_acc = 0
    count = 0
    for img, label in tqdm(self.loaders['train']):
      img = img.to(self.device)
      label = label.to(self.device)
      bsz = len(label)
      with torch.no_grad():
        output = self.model(img)
      mls_closed += torch.amax(output, dim=-1).cpu().numpy().tolist()
      pred = torch.argmax(output, dim=-1)
      label = torch.argmax(label, dim=-1)
      total_acc += torch.sum(pred == label).item()
      count += bsz
    acc = total_acc / count
    self.mls_closed_stats = {
      'mean': np.mean(mls_closed),
      'std': np.std(mls_closed)
    }
    print("\nTrain Classification Accuracy = ", acc)
    print(f'MLS stats: mean={self.mls_closed_stats["mean"]}, std={self.mls_closed_stats["std"]}')
  
  def detect_openset(self, sample):
    sample = sample.to(self.device)
    with torch.no_grad():
      output = self.model(sample)
    tau = (torch.amax(output, dim=-1) - self.mls_closed_stats['mean']) / self.mls_closed_stats['std']
    return (torch.abs(tau) > 2).bool().cpu().numpy()
  
  def openset_evaluation(self, testloader, openloader):
    self.model.eval()
    acc = 0
    count = 0
    print("CLOSED DATA")
    for img, label in tqdm(testloader):
      img = img.to(self.device)
      label = label.to(self.device)
      bsz = len(label)
      with torch.no_grad():
        if_open = self.detect_openset(img)
      acc += np.sum(~if_open)
      count += len(if_open)
    print("OPEN DATA")
    for img, label in tqdm(openloader):
      img = img.to(self.device)
      label = label.to(self.device)
      bsz = len(label)
      with torch.no_grad():
        if_open = self.detect_openset(img)
      acc += np.sum(if_open)
      count += len(if_open)

    print('Openset recognition accuracy = ', acc / count)
    return acc / count
  
  def openset_best_separator_evaluation(self, test_loader, open_loader):
    self.model.eval()

    mls_closed = []
    for img, label in tqdm(test_loader):
      img = img.to(self.device)
      label = label.to(self.device)
      bsz = len(label)
      with torch.no_grad():
        output = self.model(img)
      mls_closed += torch.amax(output, dim=-1).cpu().numpy().tolist()
    
    mls_open = []
    for img, label in tqdm(open_loader):
      img = img.to(self.device)
      label = label.to(self.device)
      bsz = len(label)
      with torch.no_grad():
        output = self.model(img)
      mls_open += torch.amax(output, dim=-1).cpu().numpy().tolist()

    x = sorted(mls_open + mls_closed)
    min_item = 0
    min_value = 1000
    for node in x:
      e1 = len([item for item in mls_open if item >= node])
      e2 = len([item for item in mls_closed if item < node])
      if e1 + e2 < min_value:
        min_value = e1 + e2
        min_item = node
    print("Best Separator Accuracy = ", (1 - min_value / len(x)) * 100)

if __name__ == '__main__':
  data_path = 'data/'
  openset_model = OpensetRecognizer(data_path='data/', feature_dim=128)
  losses, accs = openset_model.train_model(n_epochs=50)
  json.dump({'accs': accs, 'losses': losses}, open('stats.json', 'w'))

  test_data = pickle.load(open(os.path.join(data_path, 'test_data.pickle'), 'rb'))
  test_labels = pickle.load(open(os.path.join(data_path, 'test_labels.pickle'), 'rb'))

  open_data = pickle.load(open(os.path.join(data_path, 'opendata.pickle'), 'rb'))
  open_labels = pickle.load(open(os.path.join(data_path, 'open_labels.pickle'), 'rb'))

  open_labels_set = sorted(list(set(open_labels)))
  test_labels_set = sorted(list(set(test_labels)))
  test_labels_mapping = {test_labels_set[i]:i for i in range(len(test_labels_set))}
  open_labels_mapping = {open_labels_set[i]:i + len(test_labels_set) for i in range(len(open_labels_set))}

  test_data = np.stack(test_data)
  open_data = np.stack(open_data)
  test_labels = np.array([test_labels_mapping[item] for item in test_labels])
  open_labels = np.array([open_labels_mapping[item] for item in open_labels])

  test_dataset = ImageDataset(test_data, test_labels, n_classes=7, ls_eps=0, transform=openset_model.val_transform)
  test_loader = DataLoader(test_dataset, batch_size=4, shuffle=False, drop_last=False, collate_fn=openset_model.val_collate)

  open_dataset = ImageDataset(open_data, test_labels, n_classes=7, ls_eps=0, transform=openset_model.val_transform)
  open_loader = DataLoader(open_dataset, batch_size=4, shuffle=False, drop_last=False, collate_fn=openset_model.val_collate)

  openset_model.prepare_detection_model()
  openset_model.openset_evaluation(test_loader, open_loader)
  openset_model.openset_best_separator_evaluation(test_loader, open_loader)