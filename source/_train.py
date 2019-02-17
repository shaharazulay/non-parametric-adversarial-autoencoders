import torch
import itertools
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from _model import Q_net, P_net, D_net_cat, D_net_gauss
from _train_utils import *

cuda = torch.cuda.is_available()
seed = 10
pixelwise_loss = torch.nn.L1Loss()


def _train_epoch(
    models, optimizers, train_labeled_loader, train_unlabeled_loader, n_classes, z_dim):
    '''
    Train procedure for one epoch.
    '''
    TINY = 1e-15
    # load models and optimizers
    P, Q, D_cat, D_gauss = models
    P_decoder_optim, Q_encoder_optim, Q_classifier_optim, Q_regularization_optim, D_cat_optim, D_gauss_optim = optimizers

    # Set the networks in train mode (apply dropout when needed)
    train_all(P, Q, D_cat, D_gauss)

    batch_size = train_labeled_loader.batch_size

    # Loop through the labeled and unlabeled dataset getting one batch of samples from each
    for (X_l, target_l), (X_u, target_u) in itertools.izip(train_labeled_loader, train_unlabeled_loader):

        for X, target in [(X_u, target_u), (X_l, target_l)]:
            if target[0] == -1:
                labeled = False
            else:
                labeled = True

            X.resize_(batch_size, Q.input_size)

            X, target = Variable(X), Variable(target)
            if cuda:
                X, target = X.cuda(), target.cuda()

            # Init gradients
            zero_grad_all(P, Q, D_cat, D_gauss)

            #######################
            # Reconstruction phase
            #######################
            if not labeled:
                latent_vec = torch.cat(Q(X), 1)
                X_rec = P(latent_vec)

                recon_loss = F.binary_cross_entropy(X_rec + TINY, X + TINY)
                #recon_loss = pixelwise_loss(X, X_rec)

                recon_loss.backward()
                P_decoder_optim.step()
                Q_encoder_optim.step()

                # Init gradients
                zero_grad_all(P, Q, D_cat, D_gauss)

                #######################
                # Regularization phase
                #######################
                # Discriminator
                Q.eval()
                z_real_cat = sample_categorical(batch_size, n_classes=n_classes)
                z_real_gauss = Variable(torch.randn(batch_size, z_dim))
                if cuda:
                    z_real_cat = z_real_cat.cuda()
                    z_real_gauss = z_real_gauss.cuda()

                z_fake_cat, z_fake_gauss = Q(X)

                D_real_cat = D_cat(z_real_cat)
                D_real_gauss = D_gauss(z_real_gauss)
                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                D_loss_cat = - torch.mean(torch.log(D_real_cat + TINY) + torch.log(1 - D_fake_cat + TINY))
                D_loss_gauss = - torch.mean(torch.log(D_real_gauss + TINY) + torch.log(1 - D_fake_gauss + TINY))

                D_loss = D_loss_cat + D_loss_gauss
                D_loss = D_loss

                D_loss.backward()
                D_cat_optim.step()
                D_gauss_optim.step()

                # Init gradients
                zero_grad_all(P, Q, D_cat, D_gauss)

                # Generator
                Q.train()
                z_fake_cat, z_fake_gauss = Q(X)

                D_fake_cat = D_cat(z_fake_cat)
                D_fake_gauss = D_gauss(z_fake_gauss)

                G_loss = - torch.mean(torch.log(D_fake_cat + TINY)) - torch.mean(torch.log(D_fake_gauss + TINY))
                G_loss = G_loss
                G_loss.backward()
                Q_regularization_optim.step()

                # Init gradients
                zero_grad_all(P, Q, D_cat, D_gauss)

            #######################
            # Semi-supervised phase
            #######################
            if labeled:
                pred, _ = Q(X)
                class_loss = F.cross_entropy(pred, target)
                class_loss.backward()
                Q_classifier_optim.step()

                # Init gradients
                zero_grad_all(P, Q, D_cat, D_gauss)

    return D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss


def train(train_labeled_loader, train_unlabeled_loader, valid_loader, epochs, n_classes, z_dim):
    torch.manual_seed(10)

    if cuda:
        Q = Q_net().cuda()
        P = P_net().cuda()
        D_cat = D_net_cat().cuda()
        D_gauss = D_net_gauss().cuda()
    else:
        Q = Q_net()
        P = P_net()
        D_gauss = D_net_gauss()
        D_cat = D_net_cat()

    # Set learning rates
    auto_encoder_lr = 0.0006
    regularization_lr = 0.0008
    classifier_lr = 0.001

    # Set optimizators
    P_decoder_optim = optim.Adam(P.parameters(), lr=auto_encoder_lr)
    Q_encoder_optim = optim.Adam(Q.parameters(), lr=auto_encoder_lr)

    Q_regularization_optim = optim.Adam(Q.parameters(), lr=regularization_lr)
    D_gauss_optim = optim.Adam(D_gauss.parameters(), lr=regularization_lr)
    D_cat_optim = optim.Adam(D_cat.parameters(), lr=regularization_lr)

    Q_classifier_optim = optim.Adam(Q.parameters(), lr=classifier_lr)


    models = P, Q, D_cat, D_gauss
    optimizers = P_decoder_optim, Q_encoder_optim, Q_classifier_optim, Q_regularization_optim, D_cat_optim, D_gauss_optim

    for epoch in range(epochs):
        D_loss_cat, D_loss_gauss, G_loss, recon_loss, class_loss = _train_epoch(
            models,
            optimizers,
            train_labeled_loader,
            train_unlabeled_loader,
            n_classes,
            z_dim)

        if epoch % 10 == 0:
            train_acc = classification_accuracy(Q, train_labeled_loader)
            val_acc = classification_accuracy(Q, valid_loader)
            report_loss(epoch, D_loss_cat, D_loss_gauss, G_loss, recon_loss)
            print('Classification Loss: {:.3}'.format(class_loss.item()))
            print('Train accuracy: {} %'.format(train_acc))
            print('Validation accuracy: {} %'.format(val_acc))

    return Q, P