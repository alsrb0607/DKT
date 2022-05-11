import math
import os

import numpy as np
import torch
import wandb

from .criterion import get_criterion
from .dataloader import get_loaders
from .metric import get_metric
from .model import LSTM, LSTMATTN, Bert
from .optimizer import get_optimizer
from .scheduler import get_scheduler


def run(args, train_data, valid_data):
    train_loader, valid_loader = get_loaders(args, train_data, valid_data) #dkt_dataset 통과

    # only when using warmup scheduler
    args.total_steps = int(math.ceil(len(train_loader.dataset) / args.batch_size)) * (
        args.n_epochs
    )
    args.warmup_steps = args.total_steps // 10

    model = get_model(args) # 모델 받아오기
    # print('model', type(model))


    optimizer = get_optimizer(model, args)
    scheduler = get_scheduler(optimizer, args)

    best_auc = -1
    early_stopping_counter = 0

    # --- 여기 이전 단계들은 한번만 수행된다는 것: 여기 이전단계에서 임베딩을 끝냇어야 된다는 것.
    for epoch in range(args.n_epochs):

        print(f"Start Training: Epoch {epoch + 1}")

        ### TRAIN
        train_auc, train_acc, train_loss = train(
            train_loader, model, optimizer, scheduler, args
        )

        ### VALID
        auc, acc = validate(valid_loader, model, args)

        ### TODO: model save or early stopping
        wandb.log(
            {
                "epoch": epoch,
                "train_loss": train_loss,
                "train_auc": train_auc,
                "train_acc": train_acc,
                "valid_auc": auc,
                "valid_acc": acc,
            }
        )
        if auc > best_auc:
            best_auc = auc
            # torch.nn.DataParallel로 감싸진 경우 원래의 model을 가져옵니다.
            model_to_save = model.module if hasattr(model, "module") else model
            save_checkpoint(
                {
                    "epoch": epoch + 1,
                    "state_dict": model_to_save.state_dict(),
                },
                args.model_dir,
                args.model_name,
            )
            early_stopping_counter = 0
        else:
            early_stopping_counter += 1
            if early_stopping_counter >= args.patience:
                print(
                    f"EarlyStopping counter: {early_stopping_counter} out of {args.patience}"
                )
                break

        # scheduler
        if args.scheduler == "plateau":
            scheduler.step(best_auc)

def run_pesudo(args, train_data, valid_data):
    train_loader, valid_loader = get_loaders(args, train_data, valid_data)

    # only when using warmup scheduler
    args.total_steps = int(math.ceil(len(train_loader.dataset) / args.batch_size)) * (
        args.n_epochs
    )
    args.warmup_steps = args.total_steps // 10

    model = load_model(args)
    optimizer = get_optimizer(model, args)
    scheduler = get_scheduler(optimizer, args)

    best_auc = -1
    early_stopping_counter = 0
    for epoch in range(args.n_epochs):

        print(f"Start Training: Epoch {epoch + 1}")

        ### TRAIN
        train_auc, train_acc, train_loss = train(
            train_loader, model, optimizer, scheduler, args
        )

        ### VALID
        auc, acc = validate(valid_loader, model, args)

        ### TODO: model save or early stopping
        wandb.log(
            {
                "epoch": epoch,
                "train_loss": train_loss,
                "train_auc": train_auc,
                "train_acc": train_acc,
                "valid_auc": auc,
                "valid_acc": acc,
            }
        )
        model_to_save = model.module if hasattr(model, "module") else model
        save_checkpoint(
            {
                "epoch": epoch + 1,
                "state_dict": model_to_save.state_dict(),
            },
            args.model_dir,
            "model.pt",
        )
        # scheduler
        if args.scheduler == "plateau":
            scheduler.step(best_auc)



def train(train_loader, model, optimizer, scheduler, args):
    model.train() # model 에게 model 을 훈련시킬것을 알림, mode == train 을 알림

    total_preds = []
    total_targets = []
    losses = []
    for step, batch in enumerate(train_loader):
        input = process_batch(batch, args)

        preds = model(input) # lstmattn forward


        targets = input[-1]  # correct

        loss = compute_loss(preds, targets)
        update_params(loss, model, optimizer, scheduler, args) # 파라미터 업데이트 하는 부분

        if step % args.log_steps == 0:
            print(f"Training steps: {step} Loss: {str(loss.item())}")

        # predictions
        preds = preds[:, -1]
        targets = targets[:, -1]

        if args.device == "cuda":
            preds = preds.to("cpu").detach().numpy()
            targets = targets.to("cpu").detach().numpy()
        else:  # cpu
            preds = preds.detach().numpy()
            targets = targets.detach().numpy()

        total_preds.append(preds)
        total_targets.append(targets)
        losses.append(loss)

    total_preds = np.concatenate(total_preds)
    total_targets = np.concatenate(total_targets)

    # Train AUC / ACC
    auc, acc = get_metric(total_targets, total_preds)
    loss_avg = sum(losses) / len(losses)
    print(f"TRAIN AUC : {auc} ACC : {acc}")
    return auc, acc, loss_avg



def validate(valid_loader, model, args):
    model.eval()

    total_preds = []
    total_targets = []
    for step, batch in enumerate(valid_loader):
        input = process_batch(batch, args)

        preds = model(input)
        targets = input[-1]  # correct

        # predictions
        preds = preds[:, -1]
        targets = targets[:, -1]

        if args.device == "cuda":
            preds = preds.to("cpu").detach().numpy()
            targets = targets.to("cpu").detach().numpy()
        else:  # cpu
            preds = preds.detach().numpy()
            targets = targets.detach().numpy()

        total_preds.append(preds)
        total_targets.append(targets)

    total_preds = np.concatenate(total_preds)
    total_targets = np.concatenate(total_targets)

    # Train AUC / ACC
    auc, acc = get_metric(total_targets, total_preds)

    print(f"VALID AUC : {auc} ACC : {acc}\n")

    return auc, acc


def inference(args, test_data):

    model = load_model(args)
    model.eval()
    _, test_loader = get_loaders(args, None, test_data)

    total_preds = []

    for step, batch in enumerate(test_loader):
        input = process_batch(batch, args)

        preds = model(input)

        # predictions
        preds = preds[:, -1]

        if args.device == "cuda":
            preds = preds.to("cpu").detach().numpy()
        else:  # cpu
            preds = preds.detach().numpy()

        total_preds += list(preds)

    write_path = os.path.join(args.output_dir, args.output_name)
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
    with open(write_path, "w", encoding="utf8") as w:
        w.write("id,prediction\n")
        for id, p in enumerate(total_preds):
            w.write("{},{}\n".format(id, p))


def get_model(args):
    """
    Load model and move tensors to a given devices.
    """
    if args.model == "lstm":
        model = LSTM(args)
        model.to(args.device)
    if args.model == "lstmattn":
        model = LSTMATTN(args)
        model.to(args.device)
    if args.model == "bert":
        model = Bert(args)
        model.to(args.device)



    return model


# 배치 전처리 -> 어떻게 나오는지
# 여기서 처리를 하래
def process_batch(batch, args):
    # print('batch_len',len(batch)) # 4
    # batch : type -> list
    # print('batch[0]', batch[0].shape)#-> torch.size([32, 100])
    # DKTDataset 처리를 거친 상태의 batch



    cate_features = batch[:args.n_cate_feat]
    cont_features = batch[args.n_cate_feat:-2]
    mask = batch[-2]
    correct = batch[-1]

    # print(correct.shape) --> torch.Size([32, 100])

    # change to float
    mask = mask.type(torch.FloatTensor)
    correct = correct.type(torch.FloatTensor)

    # interaction을 임시적으로 correct를 한칸 우측으로 이동한 것으로 사용
    # print(correct.shape) --> torch.Size([32, 100])



    interaction = correct + 1  # 패딩을 위해 correct값에 1을 더해준다.
    interaction = interaction.roll(shifts=1, dims=1)
    interaction_mask = mask.roll(shifts=1, dims=1)
    interaction_mask[:, 0] = 0
    interaction = (interaction * interaction_mask).to(torch.int64)

    # cate_feature 설정
    cate_features = [((feature + 1) * mask).to(torch.int64) for feature in cate_features]
    # print(len(cate_features)) --> 4
    # print(cate_features[0].shape) --> torch.Size([32, 100])

    # cont_feature 설정
    if len(cont_features) != 0:
        for i in range(len(cont_features)):
            cont_features[i] = (cont_features[i]+1) * mask
            cont_features[i] = cont_features[i].unsqueeze(-1) # 2 차원을 3차원 tensor로 변환

        # print(cont_features[0].shape) --> torch.Size([32, 100, 1])

        cont_features = torch.cat(cont_features, 2)
        # print(cont_features.shape) --> torch.Size([32, 100, 8])
        cont_features = cont_features.to(torch.float32).to(args.device)
    
    # 마지막 sequence만 사용하기 위한 index
    gather_index = torch.tensor(np.count_nonzero(mask, axis=1))
    gather_index = gather_index.view(-1, 1) - 1
    
    # device memory로 이동
    cate_features = [feature.to(args.device) for feature in cate_features]

    correct = correct.to(args.device)
    mask = mask.to(args.device)
    interaction = interaction.to(args.device)
    gather_index = gather_index.to(args.device)

    # automatically tupled
    output = *cate_features, cont_features, mask, interaction, gather_index, correct
    
    return output # type: tuple


# loss계산하고 parameter update!
def compute_loss(preds, targets):
    """
    Args :
        preds   : (batch_size, max_seq_len)
        targets : (batch_size, max_seq_len)

    """
    loss = get_criterion(preds, targets)

    # 마지막 시퀀스에 대한 값만 loss 계산
    loss = loss[:, -1]
    loss = torch.mean(loss)
    return loss


def update_params(loss, model, optimizer, scheduler, args):
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad)
    if args.scheduler == "linear_warmup":
        scheduler.step()
    optimizer.step()
    optimizer.zero_grad()


def save_checkpoint(state, model_dir, model_filename):
    print("saving model ...")
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)
    torch.save(state, os.path.join(model_dir, model_filename))


def load_model(args):

    model_path = os.path.join(args.model_dir, args.model_name)
    print("Loading Model from:", model_path)
    load_state = torch.load(model_path)
    model = get_model(args)

    # load model state
    model.load_state_dict(load_state["state_dict"], strict=True)

    print("Loading Model from:", model_path, "...Finished.")
    return model
