#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Created on June 22, 2022
@author: Justin Xu
"""

import os
import sys
import csv
import time
import glob
import socket
import random
import numpy as np
import pandas as pd
from google.cloud import bigquery

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torchvision
import torchvision.transforms as transforms
from torch.utils.data import Dataset, DataLoader
import plotly
import matplotlib.pyplot as plt
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split, KFold, StratifiedKFold, GridSearchCV
from sklearn.feature_selection import RFE, VarianceThreshold
from sklearn.metrics import average_precision_score, roc_auc_score, roc_curve, accuracy_score, confusion_matrix
from functools import partial


def load_excel_data(path, sheet=0):
    filename = os.path.basename(path).strip()
    if isinstance(sheet, str):
        print(f'Loading {filename}, Sheet: {sheet}...')
    else:
        print('Loading ' + filename + '...')
    df_data = pd.read_excel(path, sheet)
    print("Done.")
    return df_data


def load_image_data(path, patients, limit=False):
    data_images = {}
    for root, dirs, files in os.walk(path):
        dirs.sort(key=int)
        dirs = list(map(int, dirs))
        dirs = [patient for patient in dirs if patient in patients]
        if limit:
            dirs = dirs[:limit]
        for d in dirs:
            print(f"Loading Patient {d}...")
            np_filenames = glob.glob(f"{os.path.join(root, f'{d}')}/*/*.npy")
            data_images[d] = [np.load(np_filenames[0]), np.load(np_filenames[1])]
        break
    return data_images, dirs


def random_seed(seed_value, use_cuda):
    np.random.seed(seed_value)  # set np random seed
    torch.manual_seed(seed_value)  # set torch seed
    random.seed(seed_value)  # set python random seed
    if use_cuda:
        torch.cuda.manual_seed(seed_value)
        torch.cuda.manual_seed_all(seed_value)
        # reproducibility
        torch.use_deterministic_algorithms(True)
        torch.backends.cudnn.benchmark = False


###############################################################################
# Plot Training Curve
def plot_training_curve(path):
    """ Plots the training curve for a model run, given the csv files
    containing the train/validation error/loss.

    Args:
        path: The base path of the csv files produced during training
    """
    train_err = np.loadtxt("{}_train_err.csv".format(path))
    val_err = np.loadtxt("{}_val_err.csv".format(path))
    train_loss = np.loadtxt("{}_train_loss.csv".format(path))
    val_loss = np.loadtxt("{}_val_loss.csv".format(path))
    plt.title("Train vs Validation Error")
    n = len(train_err)  # number of epochs
    plt.plot(range(1, n + 1), train_err, label="Train")
    plt.plot(range(1, n + 1), val_err, label="Validation")
    plt.xlabel("Epoch")
    plt.ylabel("Error")
    plt.legend(loc='best')
    plt.show()
    plt.title("Train vs Validation Loss")
    plt.plot(range(1, n + 1), train_loss, label="Train")
    plt.plot(range(1, n + 1), val_loss, label="Validation")
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.legend(loc='best')
    plt.show()


###############################################################################
# Model Classes
class CNNDataset(Dataset):
    def __init__(self, data, patient_ids):
        self.data = data
        self.patient_ids = patient_ids

    def __len__(self):
        return len(self.patient_ids)

    def __getitem__(self, idx):
        return self.data[self.patient_ids[idx]]["input"], self.data[self.patient_ids[idx]]["label"]


def conv3x3x3(in_planes, out_planes, stride=1):
    return nn.Conv3d(in_planes,
                     out_planes,
                     kernel_size=3,
                     stride=stride,
                     padding=1,
                     bias=False)


def conv1x1x1(in_planes, out_planes, stride=1):
    return nn.Conv3d(in_planes,
                     out_planes,
                     kernel_size=1,
                     stride=stride,
                     bias=False)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_planes, planes, stride=1, downsample=None):
        super().__init__()

        self.name = "BasicBlock"
        self.conv1 = conv3x3x3(in_planes, planes, stride)
        self.bn1 = nn.BatchNorm3d(planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3x3(planes, planes)
        self.bn2 = nn.BatchNorm3d(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, in_planes, planes, stride=1, downsample=None):
        super().__init__()

        self.name = "Bottleneck"
        self.conv1 = conv1x1x1(in_planes, planes)
        self.bn1 = nn.BatchNorm3d(planes)
        self.conv2 = conv3x3x3(planes, planes, stride)
        self.bn2 = nn.BatchNorm3d(planes)
        self.conv3 = conv1x1x1(planes, planes * self.expansion)
        self.bn3 = nn.BatchNorm3d(planes * self.expansion)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class ResNet(nn.Module):

    def __init__(self,
                 block,
                 layers,
                 block_inplanes,
                 model_depth,
                 n_input_channels=3,
                 conv1_t_size=7,
                 conv1_t_stride=1,
                 no_max_pool=False,
                 shortcut_type='B',
                 widen_factor=1.0,
                 n_classes=400):
        super().__init__()

        block_inplanes = [int(x * widen_factor) for x in block_inplanes]

        self.name = f"ResNet_pLGG_Classifer_depth{model_depth}"

        self.in_planes = block_inplanes[0]
        self.no_max_pool = no_max_pool

        self.conv1 = nn.Conv3d(n_input_channels,
                               self.in_planes,
                               kernel_size=(conv1_t_size, 7, 7),
                               stride=(conv1_t_stride, 2, 2),
                               padding=(conv1_t_size // 2, 3, 3),
                               bias=False)
        self.bn1 = nn.BatchNorm3d(self.in_planes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool3d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, block_inplanes[0], layers[0],
                                       shortcut_type)
        self.layer2 = self._make_layer(block,
                                       block_inplanes[1],
                                       layers[1],
                                       shortcut_type,
                                       stride=2)
        self.layer3 = self._make_layer(block,
                                       block_inplanes[2],
                                       layers[2],
                                       shortcut_type,
                                       stride=2)
        self.layer4 = self._make_layer(block,
                                       block_inplanes[3],
                                       layers[3],
                                       shortcut_type,
                                       stride=2)

        self.avgpool = nn.AdaptiveAvgPool3d((1, 1, 1))
        self.fc = nn.Linear(block_inplanes[3] * block.expansion, n_classes)
        self.dropout = nn.Dropout(dropout_rate)

        for m in self.modules():
            if isinstance(m, nn.Conv3d):
                nn.init.kaiming_normal_(m.weight,
                                        mode='fan_out',
                                        nonlinearity='relu')
            elif isinstance(m, nn.BatchNorm3d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def _downsample_basic_block(self, x, planes, stride):
        out = F.avg_pool3d(x, kernel_size=1, stride=stride)
        zero_pads = torch.zeros(out.size(0), planes - out.size(1), out.size(2),
                                out.size(3), out.size(4))
        if isinstance(out.data, torch.cuda.FloatTensor):
            zero_pads = zero_pads.cuda()

        out = torch.cat([out.data, zero_pads], dim=1)

        return out

    def _make_layer(self, block, planes, blocks, shortcut_type, stride=1):
        downsample = None
        if stride != 1 or self.in_planes != planes * block.expansion:
            if shortcut_type == 'A':
                downsample = partial(self._downsample_basic_block,
                                     planes=planes * block.expansion,
                                     stride=stride)
            else:
                downsample = nn.Sequential(
                    conv1x1x1(self.in_planes, planes * block.expansion, stride),
                    nn.BatchNorm3d(planes * block.expansion))

        layers = []
        layers.append(
            block(in_planes=self.in_planes,
                  planes=planes,
                  stride=stride,
                  downsample=downsample))
        self.in_planes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.in_planes, planes))

        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.dropout(x)
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        if not self.no_max_pool:
            x = self.maxpool(x)

        x = self.layer1(x)
        x = self.dropout(x)
        x = self.layer2(x)
        x = self.dropout(x)
        x = self.layer3(x)
        x = self.dropout(x)
        x = self.layer4(x)
        x = self.dropout(x)

        x = self.avgpool(x)

        x = x.view(x.size(0), -1)
        x = self.fc(x)
        x = torch.sigmoid(x)

        return x


def generate_model(model_depth, inplanes, **kwargs):
    assert model_depth in [10, 18, 34, 50, 101, 152, 200]
    model = None
    if model_depth == 10:
        model = ResNet(BasicBlock, [1, 1, 1, 1], inplanes, model_depth, **kwargs)
    elif model_depth == 18:
        model = ResNet(BasicBlock, [2, 2, 2, 2], inplanes, model_depth, **kwargs)
    elif model_depth == 34:
        model = ResNet(BasicBlock, [3, 4, 6, 3], inplanes, model_depth, **kwargs)
    elif model_depth == 50:
        model = ResNet(Bottleneck, [3, 4, 6, 3], inplanes, model_depth, **kwargs)
    elif model_depth == 101:
        model = ResNet(Bottleneck, [3, 4, 23, 3], inplanes, model_depth, **kwargs)
    elif model_depth == 152:
        model = ResNet(Bottleneck, [3, 8, 36, 3], inplanes, model_depth, **kwargs)
    elif model_depth == 200:
        model = ResNet(Bottleneck, [3, 24, 36, 3], inplanes, model_depth, **kwargs)

    return model


def get_model_name(name, batch_size, learning_rate, dropout_rate, epoch):
    """ Generate a name for the model consisting of all the hyperparameter values

    Args:
        config: Configuration object containing the hyperparameters
    Returns:
        path: A string with the hyperparameter name and value concatenated
    """
    path = "model_{0}_bs{1}_lr{2}_dr{3}_epoch{4}".format(name,
                                                         batch_size,
                                                         learning_rate,
                                                         dropout_rate,
                                                         epoch)
    return path


###############################################################################
# Model Training
def evaluate(net, loader, criterion):
    """ Evaluate the network on a data set.

     Args:
         net: PyTorch neural network object
         loader: PyTorch data loader for the validation set
         criterion: The loss function
     Returns:
         err: A scalar for the avg classification error over the validation set
         loss: A scalar for the average loss function over the validation set
     """
    total_err = 0.0
    total_loss = 0.0
    total_epoch = 0
    net.eval()
    with torch.set_grad_enabled(False):
        true = []
        estimated = []
        for inputs, labels in loader:
            # Transfer to GPU
            inputs, labels = inputs.to(device), labels.to(device)
            outputs = net(inputs)
            loss = criterion(outputs, labels.float())
            corr = (outputs > 0.0).squeeze().long() != labels
            total_err += int(corr.sum())
            total_loss += loss.item()
            total_epoch += len(labels)

            for i in range(len(labels.tolist())):
                true.append(labels.tolist()[i][0])
                estimated.append(outputs.tolist()[i][0])

        auc = roc_auc_score(true, estimated)

    err = float(total_err) / total_epoch
    loss = float(total_loss) / (i + 1)
    return err, loss, auc


def train_net(net, optimizer, criterion, batch_size=64, learning_rate=0.01, num_epochs=30):
    train_err = np.zeros(num_epochs)
    train_loss = np.zeros(num_epochs)
    val_err = np.zeros(num_epochs)
    val_loss = np.zeros(num_epochs)

    training_start_time = time.time()

    for epoch in range(num_epochs):
        total_train_loss = 0.0
        total_train_err = 0.0
        total_epoch = 0

        net.train()
        train_loss = 0
        train_batches = 0
        training_true = []
        training_estimated = []

        for inputs, label in train_dataloader:
            # Put data on GPU
            inputs = inputs.to(device)
            label = label.to(device)

            # Add noise to images
            noise = torch.randn_like(inputs, device=device) * 0.1
            inputs = inputs + noise

            # Forward + Backward + Optimize
            optimizer.zero_grad()
            output = net(inputs)
            loss = criterion(output, label)
            loss.backward()
            optimizer.step()
            if use_scheduler:
                scheduler.step()

            for i in range(len(label.tolist())):
                training_true.append(label.tolist()[i][0])
                training_estimated.append(output.tolist()[i][0])

            # Keep track of loss through the entire epoch
            train_loss += loss.item() * batch_size
            train_batches += 1

        # Calculate average loss over epoch
        train_loss = train_loss / (train_batches * batch_size)

        # Get results-flair on the validation and test sets
        net.eval()
        with torch.set_grad_enabled(False):
            # Validation
            val_loss = 0
            batches = 0
            validation_true = []
            validation_estimated = []
            validation_radiomics_estimated = []
            for inputs, label in validation_dataloader:
                # Transfer to GPU
                inputs, label = inputs.to(device), label.to(device)
                output = net(inputs)
                for i in range(len(label.tolist())):
                    validation_true.append(label.tolist()[i][0])
                    validation_estimated.append(output.tolist()[i][0])
                val_loss += criterion(output, label).item() * batch_size
                batches += 1
            val_loss = val_loss / (batches * batch_size)

        # Calculate the AUC for the different models
        val_auc = roc_auc_score(validation_true, validation_estimated)
        train_auc = roc_auc_score(training_true, training_estimated)


########################################################################
# Other functions
def create_label(mutation, fusion):
    if mutation == 1:
        return 1
    elif fusion == 1:
        return 0
    else:
        return None


def process_excel(df_data, exclusions):
    nanmask = np.isnan(df_data["code"])
    data_data_new = df_data[~nanmask]
    data_data_new = data_data_new.reindex()

    # Remove exluded patients
    data_data_new = data_data_new[~data_data_new["code"].isin(exclusions)]
    data_data_new = data_data_new.reindex()

    # Remove data that we don't need for this analysis
    data_data_new = data_data_new.drop(columns=['WT', 'NF1',
                                                'CDKN2A (0=balanced, 1=Del, 2=Undetermined)', 'FGFR 1', 'FGFR 2',
                                                'FGFR 4',
                                                'Further gen info', 'Notes', 'Pathology Dx_Original', 'Pathology Coded',
                                                'Location_1', 'Location_2', 'Location_Original', 'Gender', 'Age Dx'])

    data_data_new['label'] = data_data_new.apply(lambda x: create_label(x['BRAF V600E final'], x['BRAF fusion final']),
                                                 axis=1)
    data_data_new = data_data_new.drop(columns=["BRAF V600E final", "BRAF fusion final"])

    # Drop rows where the outcome is not mutation or fusion
    nanmask = np.isnan(data_data_new["label"])
    data_data_new = data_data_new[~nanmask]
    data_data_new = data_data_new.reindex()
    patient_codes = [int(x) for x in list(data_data_new["code"].values)]

    training_labels = dict(zip(patient_codes, list(data_data_new["label"].values)))
    data_data_new = data_data_new.drop(columns=["label"])

    # Organize the radiomic features into a dictionary with patient codes and corresponding patient features
    data_data_new.set_index("code", inplace=True)
    radiomic_features = {}
    for index, row in data_data_new.iterrows():
        radiomic_features[index] = row.values
    return radiomic_features, training_labels


########################################################################

# run
if __name__ == '__main__':
    start_up_time = time.time()
    random_seed(1, True)
    pd.set_option('display.max_rows', None)

    # use numpy files instead of .nii
    # no need to normalize images between [0,1] as input images are already preprocessed
    # https://github.com/kenshohara/3D-ResNets-PyTorch

    radiomics_directory = r'C:\Users\Justin\Documents\Data'
    image_directory = r'K:\Projects\SickKids_Brain_Preprocessing\preprocessed_FLAIR_from_tumor_seg_dir'

    # Parameters
    load_model = False
    use_scheduler = False
    limit = 5

    num_trials = 5
    num_epochs = 2
    batch_size = 8
    learning_rate = 0.01
    dropout_rate = 0.5  # default
    inplanes = [64, 128, 256, 512]

    excluded_patients = [9.0, 12.0, 23.0, 37.0, 58.0, 74.0, 78.0, 85.0, 121.0, 122.0, 130.0, 131.0, 138.0, 140.0, 150.0,
                         171.0, 176.0, 182.0, 204.0, 213.0, 221.0, 224.0, 234.0, 245.0, 246.0, 274.0, 306.0, 311.0,
                         312.0, 330.0, 334.0, 347.0, 349.0, 352.0, 354.0, 359.0, 364.0, 377.0,
                         235.0, 243.0, 255.0, 261.0, 264.0, 283.0, 288.0, 293.0,
                         299.0, 309.0, 325.0, 327.0, 333.0, 334.0, 356.0, 367.0,
                         376.0, 383.0, 387.0]

    if torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    # Load data
    df_sickkids = load_excel_data(os.path.join(radiomics_directory, 'Nomogram_study_LGG_data_Nov.27.xlsx'), sheet='SK')
    sickkids_radiomics_features, sickkids_labels = process_excel(df_data=df_sickkids, exclusions=excluded_patients)

    # Prepare CNN data
    radiomics_patients_list = set(sickkids_labels.keys())
    patients_with_FLAIR = []
    for each_patient in os.listdir(image_directory):
        try:
            patients_with_FLAIR.append(int(each_patient))
        except:
            print(f'Patient {each_patient} FLAIR not found.')
    patients_with_FLAIR.sort(key=int)
    patients_list = list(radiomics_patients_list.intersection(patients_with_FLAIR))
    print(f"Total number of patients: {len(patients_list)}.")
    print(f"Start-up time: {time.time() - start_up_time}\n")

    load_image_time = time.time()
    images, patients_used = load_image_data(image_directory, patients=patients_list, limit=limit)
    data = {}
    for each_patient in patients_used:
        input = torch.tensor(np.multiply(images[each_patient][0], images[each_patient][1])).float().unsqueeze(0)
    print(f"Image loading time: {time.time() - load_image_time}\n")

    if load_model:
        try:
            net = generate_model(model_depth=18, inplanes=inplanes, n_classes=1039)
            model_path = get_model_name(net.name, batch_size=batch_size, learning_rate=learning_rate,
                                        dropout_rate=dropout_rate, epoch=num_epochs)
            state = torch.load(model_path)
            net.load_state_dict(state)
        except FileNotFoundError:
            print('Model not found.')
        else:
            print("Insert code...")
        sys.exit()

    training_aucs = []
    validation_aucs = []
    test_aucs = []
    best_epochs = []
    trial_times = []

    for t in range(num_trials):
        begin_trial_time = time.time()

        # Set the seed for this iteration
        seed = 1
        random_seed(seed, True)
        next_seed = random.randint(0, 1000)
        seed = next_seed

        dataset = CNNDataset(images, patients_used)
        train_size = int(0.6 * len(dataset))
        validation_size = int(0.2 * len(dataset))
        test_size = len(dataset) - train_size - validation_size
        train_dataset, validation_dataset, test_dataset = torch.utils.data.random_split(dataset, [train_size,
                                                                                                  validation_size,
                                                                                                  test_size])
        train_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=0)
        validation_dataloader = DataLoader(validation_dataset, batch_size=batch_size, shuffle=True, num_workers=0)
        test_dataloader = DataLoader(test_dataset, batch_size=batch_size, shuffle=True, num_workers=0)
        print(f"Datasplit -> Training: {train_size}, Validation: {validation_size}, Testing: {test_size}.")

        net = generate_model(model_depth=18, inplanes=inplanes, n_classes=1039)

        net.conv1 = nn.Conv3d(1, 64, kernel_size=(7, 7, 7), stride=(1, 2, 2), padding=(3, 3, 3), bias=False)
        net.fc = net.fc = nn.Linear(512, 1)

        net.to(device)

        criterion = nn.BCELoss()
        optimizer = optim.Adam(net.parameters(), lr=learning_rate)

        if use_scheduler:
            scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=75, gamma=0.1)

        lowest_val_loss = np.inf  # Lowest loss on validation set
        lowest_val_loss_epoch = 0  # Epoch where the lowest loss on validation set was achieved (append to best_epochs)
        best_training_auc = None  # The AUC on the training set for the model that had the lowest training loss
        best_val_auc = None  # The AUC on the validation set for the model that had the lowest validation loss
        best_test_auc = None  # The AUC on the test set for the model that had the lowest validation loss

        epoch, train_err, train_loss, train_auc, val_err, val_loss, val_auc, test_auc = train_net(net=net,
                                                                                                  optimizer=optimizer,
                                                                                                  criterion=criterion,
                                                                                                  batch_size=batch_size,
                                                                                                  learning_rate=learning_rate,
                                                                                                  num_epochs=num_epochs)

        trial_duration = time.time() - begin_trial_time
        trial_times.append(trial_duration)
        print(f"Trial {t} time: {trial_duration}\n")

        print(f"Best Epoch: {epoch}, "
              f"Lowest Training Error {round(train_err, 3)}, "
              f"Lowest Training Loss {round(train_loss, 3)} , "
              f"Best Training AUC: {round(train_auc, 3)}, "
              f"Lowest Validation Error {round(val_err, 3)}, "
              f"Lowest Validation Loss {round(val_loss, 3)} , "
              f"Best Validation AUC: {round(val_auc, 3)}, "
              f"Test AUC: {round(test_auc, 3)}")

    print('Experiment done.')
print('---------------------')
