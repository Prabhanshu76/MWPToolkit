import torch
import time
from logging import getLogger
from mwptoolkit.utils.utils import time_since
from mwptoolkit.utils.enum_type import PAD_TOKEN, DatasetType
from mwptoolkit.loss.masked_cross_entropy_loss import MaskedCrossEntropyLoss
from mwptoolkit.loss.nll_loss import NLLLoss
from mwptoolkit.loss.binary_cross_entropy_loss import BinaryCrossEntropyLoss
from mwptoolkit.module.Optimizer.optim import WarmUpScheduler


class AbstractTrainer(object):
    def __init__(self, config, model, dataloader, evaluator):
        super().__init__()
        self.config = config
        self.model = model
        self.dataloader = dataloader
        self.evaluator = evaluator
        self.logger = getLogger()

        self.best_valid_equ_accuracy = 0.
        self.best_valid_value_accuracy = 0.
        self.best_test_equ_accuracy = 0.
        self.best_test_value_accuracy = 0.
        self.start_epoch = 0
        self.epoch_i = 0

    def _save_checkpoint(self):
        raise NotImplementedError

    def _load_checkpoint(self):
        raise NotImplementedError

    def _save_model(self):
        state_dict = {"model": self.model.state_dict()}
        torch.save(state_dict, self.config["trained_model_path"])

    def _load_model(self):
        state_dict = torch.load(self.config["trained_model_path"], map_location=self.config["map_location"])
        self.model.load_state_dict(state_dict["model"])

    def _build_optimizer(self):
        raise NotImplementedError

    def _train_batch(self):
        raise NotADirectoryError

    def _eval_batch(self):
        raise NotImplementedError

    def _train_epoch(self):
        raise NotImplementedError

    def fit(self):
        raise NotImplementedError

    def evaluate(self, eval_set):
        raise NotImplementedError

    def test(self):
        raise NotImplementedError


class Trainer(AbstractTrainer):
    def __init__(self, config, model, dataloader, evaluator):
        super().__init__(config, model, dataloader, evaluator)
        self._build_optimizer()
        if config["resume"]:
            self._load_checkpoint()
        self._build_loss(config["symbol_size"], self.dataloader.dataset.out_symbol2idx[PAD_TOKEN])

    def _build_optimizer(self):
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.config["learning_rate"])

    def _save_checkpoint(self):
        check_pnt = {
            "model": self.model.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "start_epoch": self.epoch_i,
            "best_valid_value_accuracy": self.best_valid_value_accuracy,
            "best_valid_equ_accuracy": self.best_valid_equ_accuracy,
            "best_test_value_accuracy": self.best_test_value_accuracy,
            "best_test_equ_accuracy": self.best_test_equ_accuracy
        }
        torch.save(check_pnt, self.config["checkpoint_path"])

    def _load_checkpoint(self):
        #check_pnt = torch.load(self.config["checkpoint_path"],map_location="cpu")
        check_pnt = torch.load(self.config["checkpoint_path"], map_location=self.config["map_location"])
        # load parameter of model
        self.model.load_state_dict(check_pnt["model"])
        # load parameter of optimizer
        self.optimizer.load_state_dict(check_pnt["optimizer"])
        # other parameter
        self.start_epoch = check_pnt["start_epoch"]
        self.best_valid_value_accuracy = check_pnt["best_valid_value_accuracy"]
        self.best_valid_equ_accuracy = check_pnt["best_valid_equ_accuracy"]
        self.best_test_value_accuracy = check_pnt["best_test_value_accuracy"]
        self.best_test_equ_accuracy = check_pnt["best_test_equ_accuracy"]

    def _build_loss(self, symbol_size, out_pad_token):
        weight = torch.ones(symbol_size).to(self.config["device"])
        pad = out_pad_token
        self.loss = NLLLoss(weight, pad)

    def _idx2word_2idx(self, batch_equation):
        batch_size, length = batch_equation.size()
        batch_equation_ = []
        for b in range(batch_size):
            equation = []
            for idx in range(length):
                equation.append(self.dataloader.dataset.out_symbol2idx[\
                                            self.dataloader.dataset.in_idx2word[\
                                                batch_equation[b,idx]]])
            batch_equation_.append(equation)
        batch_equation_ = torch.LongTensor(batch_equation_).to(self.config["device"])
        return batch_equation_

    def _train_batch(self, batch):
        outputs = self.model(batch["question"], batch["ques len"], batch["equation"])
        #outputs=torch.nn.functional.log_softmax(outputs,dim=1)
        if self.config["share_vocab"]:
            batch_equation = self._idx2word_2idx(batch["equation"])
            self.loss.eval_batch(outputs, batch_equation.view(-1))
        else:
            self.loss.eval_batch(outputs, batch["equation"].view(-1))
        batch_loss = self.loss.get_loss()
        return batch_loss

    def _eval_batch(self, batch):
        test_out = self.model(batch["question"], batch["ques len"])
        if self.config["share_vocab"]:
            target = self._idx2word_2idx(batch["equation"])
        else:
            target = batch["equation"]
        batch_size = target.size(0)
        val_acc = []
        equ_acc = []
        for idx in range(batch_size):
            val_ac, equ_ac, _, _ = self.evaluator.result(test_out[idx], target[idx], batch["num list"][idx], batch["num stack"][idx])
            val_acc.append(val_ac)
            equ_acc.append(equ_ac)
        return val_acc, equ_acc

    def _train_epoch(self):
        epoch_start_time = time.time()
        loss_total = 0.
        self.model.train()
        for batch_idx, batch in enumerate(self.dataloader.load_data(DatasetType.Train)):
            self.batch_idx = batch_idx + 1
            self.model.zero_grad()
            batch_loss = self._train_batch(batch)
            loss_total += batch_loss
            self.loss.backward()
            self.optimizer.step()
            self.loss.reset()
        epoch_time_cost = time_since(time.time() - epoch_start_time)
        return loss_total, epoch_time_cost

    def fit(self):
        train_batch_size = self.config["train_batch_size"]
        epoch_nums = self.config["epoch_nums"]

        self.train_batch_nums = int(self.dataloader.trainset_nums / train_batch_size) + 1

        for epo in range(self.start_epoch, epoch_nums):
            self.epoch_i = epo + 1
            self.model.train()
            loss_total, train_time_cost = self._train_epoch()

            self.logger.info("epoch [%3d] avr loss [%2.8f] | train time %s"\
                                %(self.epoch_i,loss_total/self.train_batch_nums,train_time_cost))

            if epo % 2 == 0 or epo > epoch_nums - 5:
                valid_equ_ac, valid_val_ac, valid_total, valid_time_cost = self.evaluate(DatasetType.Valid)

                self.logger.info("----------- valid total [%d] | valid equ acc [%2.3f] | valid value acc [%2.3f] | valid time %s"\
                                %(valid_total,valid_equ_ac,valid_val_ac,valid_time_cost))
                test_equ_ac, test_val_ac, test_total, test_time_cost = self.evaluate(DatasetType.Test)

                self.logger.info("----------- test total [%d] | test equ acc [%2.3f] | test value acc [%2.3f] | test time %s"\
                                %(test_total,test_equ_ac,test_val_ac,test_time_cost))

                if valid_val_ac >= self.best_valid_value_accuracy:
                    self.best_valid_value_accuracy = valid_val_ac
                    self.best_valid_equ_accuracy = valid_equ_ac
                    self.best_test_value_accuracy = test_val_ac
                    self.best_test_equ_accuracy = test_equ_ac
                    self._save_model()
            if epo % 5 == 0:
                self._save_checkpoint()
        self.logger.info('''training finished.
                            best valid result: equation accuracy [%2.3f] | value accuracy [%2.3f]
                            best test result : equation accuracy [%2.3f] | value accuracy [%2.3f]'''\
                            %(self.best_valid_equ_accuracy,self.best_valid_value_accuracy,\
                                self.best_test_equ_accuracyself.best_test_value_accuracy))
    
    def evaluate(self, eval_set):
        self.model.eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0
        test_start_time = time.time()

        for batch in self.dataloader.load_data(eval_set):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)

        test_time_cost = time_since(time.time() - test_start_time)
        return equation_ac / eval_total, value_ac / eval_total, eval_total, test_time_cost

    def test(self):
        self._load_model()
        self.model.eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0
        test_start_time = time.time()

        for batch in self.dataloader.load_data(DatasetType.Test):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)
        test_time_cost = time_since(time.time() - test_start_time)
        self.logger.info("test total [%d] | test equ acc [%2.3f] | test value acc [%2.3f] | test time %s"\
                                %(eval_total,equation_ac/eval_total,value_ac/eval_total,test_time_cost))


class SingleEquationTrainer(Trainer):
    def __init__(self, config, model, dataloader, evaluator):
        super().__init__(config, model, dataloader, evaluator)
        self._build_optimizer()
        if config["resume"]:
            self._load_checkpoint()
        self._build_loss(config["symbol_size"], self.dataloader.dataset.out_symbol2idx[PAD_TOKEN])

    def _build_optimizer(self):
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.config["learning_rate"])

    def _save_checkpoint(self):
        check_pnt = {
            "model": self.model.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "start_epoch": self.epoch_i,
            "best_valid_value_accuracy": self.best_valid_value_accuracy,
            "best_valid_equ_accuracy": self.best_valid_equ_accuracy,
            "best_test_value_accuracy": self.best_test_value_accuracy,
            "best_test_equ_accuracy": self.best_test_equ_accuracy
        }
        torch.save(check_pnt, self.config["checkpoint_path"])

    def _load_checkpoint(self):
        #check_pnt = torch.load(self.config["checkpoint_path"],map_location="cpu")
        check_pnt = torch.load(self.config["checkpoint_path"], map_location=self.config["map_location"])
        # load parameter of model
        self.model.load_state_dict(check_pnt["model"])
        # load parameter of optimizer
        self.optimizer.load_state_dict(check_pnt["optimizer"])
        # other parameter
        self.start_epoch = check_pnt["start_epoch"]
        self.best_valid_value_accuracy = check_pnt["best_valid_value_accuracy"]
        self.best_valid_equ_accuracy = check_pnt["best_valid_equ_accuracy"]
        self.best_test_value_accuracy = check_pnt["best_test_value_accuracy"]
        self.best_test_equ_accuracy = check_pnt["best_test_equ_accuracy"]

    def _build_loss(self, symbol_size, out_pad_token):
        weight = torch.ones(symbol_size).to(self.config["device"])
        pad = out_pad_token
        self.loss = NLLLoss(weight, pad)

    def _idx2word_2idx(self, batch_equation):
        batch_size, length = batch_equation.size()
        batch_equation_ = []
        for b in range(batch_size):
            equation = []
            for idx in range(length):
                equation.append(self.dataloader.dataset.out_symbol2idx[\
                                            self.dataloader.dataset.in_idx2word[\
                                                batch_equation[b,idx]]])
            batch_equation_.append(equation)
        batch_equation_ = torch.LongTensor(batch_equation_).to(self.config["device"])
        return batch_equation_

    def _train_batch(self, batch):
        outputs = self.model(batch["question"], batch["ques len"], batch["equation"])
        #outputs=torch.nn.functional.log_softmax(outputs,dim=1)
        if self.config["share_vocab"]:
            batch_equation = self._idx2word_2idx(batch["equation"])
            self.loss.eval_batch(outputs, batch_equation.view(-1))
        else:
            self.loss.eval_batch(outputs, batch["equation"].view(-1))
        batch_loss = self.loss.get_loss()
        return batch_loss

    def _eval_batch(self, batch):
        test_out = self.model(batch["question"], batch["ques len"])
        if self.config["share_vocab"]:
            target = self._idx2word_2idx(batch["equation"])
        else:
            target = batch["equation"]
        batch_size = target.size(0)
        val_acc = []
        equ_acc = []
        for idx in range(batch_size):
            val_ac, equ_ac, _, _ = self.evaluator.result(test_out[idx], target[idx], batch["num list"][idx], batch["num stack"][idx])
            val_acc.append(val_ac)
            equ_acc.append(equ_ac)
        return val_acc, equ_acc

    def _train_epoch(self):
        epoch_start_time = time.time()
        loss_total = 0.
        self.model.train()
        for batch_idx, batch in enumerate(self.dataloader.load_data(DatasetType.Train)):
            self.batch_idx = batch_idx + 1
            self.model.zero_grad()
            batch_loss = self._train_batch(batch)
            loss_total += batch_loss
            self.loss.backward()
            self.optimizer.step()
            self.loss.reset()
        epoch_time_cost = time_since(time.time() - epoch_start_time)
        return loss_total, epoch_time_cost

    def fit(self):
        train_batch_size = self.config["train_batch_size"]
        epoch_nums = self.config["epoch_nums"]

        self.train_batch_nums = int(self.dataloader.trainset_nums / train_batch_size) + 1

        for epo in range(self.start_epoch, epoch_nums):
            self.epoch_i = epo + 1
            self.model.train()
            loss_total, train_time_cost = self._train_epoch()
            self.logger.info("epoch [%3d] avr loss [%2.8f] | train time %s"\
                                %(self.epoch_i,loss_total/self.train_batch_nums,train_time_cost))

            if epo % 2 == 0 or epo > epoch_nums - 5:
                valid_equ_ac, valid_val_ac, valid_total, valid_time_cost = self.evaluate(DatasetType.Valid)

                self.logger.info("---------- valid total [%d] | valid equ acc [%2.3f] | valid value acc [%2.3f] | valid time %s"\
                                %(valid_total,valid_equ_ac,valid_val_ac,valid_time_cost))
                test_equ_ac, test_val_ac, test_total, test_time_cost = self.evaluate(DatasetType.Test)

                self.logger.info("---------- test total [%d] | test equ acc [%2.3f] | test value acc [%2.3f] | test time %s"\
                                %(test_total,test_equ_ac,test_val_ac,test_time_cost))

                if valid_val_ac >= self.best_valid_value_accuracy:
                    self.best_valid_value_accuracy = valid_val_ac
                    self.best_valid_equ_accuracy = valid_equ_ac
                    self.best_test_value_accuracy = test_val_ac
                    self.best_test_equ_accuracy = test_equ_ac
                    self._save_model()
            if epo % 5 == 0:
                self._save_checkpoint()
        self.logger.info('''training finished.
                            best valid result: equation accuracy [%2.3f] | value accuracy [%2.3f]
                            best test result : equation accuracy [%2.3f] | value accuracy [%2.3f]'''\
                            %(self.best_valid_equ_accuracy,self.best_valid_value_accuracy,\
                                self.best_test_equ_accuracyself.best_test_value_accuracy))
    def evaluate(self, eval_set):
        self.model.eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0
        test_start_time = time.time()

        for batch in self.dataloader.load_data(eval_set):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)
            
        test_time_cost = time_since(time.time() - test_start_time)
        return equation_ac / eval_total, value_ac / eval_total, eval_total, test_time_cost

    def test(self):
        self._load_model()
        self.model.eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0
        test_start_time = time.time()

        for batch in self.dataloader.load_data(DatasetType.Test):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)
        test_time_cost = time_since(time.time() - test_start_time)
        self.logger.info("test total [%d] | test equ acc [%2.3f] | test value acc [%2.3f] | test time %s"\
                                %(eval_total,equation_ac/eval_total,value_ac/eval_total,test_time_cost))

class MultiEquationTrainer(Trainer):
    def __init__(self, config, model, dataloader, evaluator):
        super().__init__(config, model, dataloader, evaluator)
        self._build_optimizer()
        if config["resume"]:
            self._load_checkpoint()
        self._build_loss(config["symbol_size"], self.dataloader.dataset.out_symbol2idx[PAD_TOKEN])

    def _build_optimizer(self):
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.config["learning_rate"])

    def _save_checkpoint(self):
        check_pnt = {
            "model": self.model.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "start_epoch": self.epoch_i,
            "best_valid_value_accuracy": self.best_valid_value_accuracy,
            "best_valid_equ_accuracy": self.best_valid_equ_accuracy,
            "best_test_value_accuracy": self.best_test_value_accuracy,
            "best_test_equ_accuracy": self.best_test_equ_accuracy
        }
        torch.save(check_pnt, self.config["checkpoint_path"])

    def _load_checkpoint(self):
        #check_pnt = torch.load(self.config["checkpoint_path"],map_location="cpu")
        check_pnt = torch.load(self.config["checkpoint_path"], map_location=self.config["map_location"])
        # load parameter of model
        self.model.load_state_dict(check_pnt["model"])
        # load parameter of optimizer
        self.optimizer.load_state_dict(check_pnt["optimizer"])
        # other parameter
        self.start_epoch = check_pnt["start_epoch"]
        self.best_valid_value_accuracy = check_pnt["best_valid_value_accuracy"]
        self.best_valid_equ_accuracy = check_pnt["best_valid_equ_accuracy"]
        self.best_test_value_accuracy = check_pnt["best_test_value_accuracy"]
        self.best_test_equ_accuracy = check_pnt["best_test_equ_accuracy"]

    def _build_loss(self, symbol_size, out_pad_token):
        weight = torch.ones(symbol_size).to(self.config["device"])
        pad = out_pad_token
        self.loss = NLLLoss(weight, pad)

    def _idx2word_2idx(self, batch_equation):
        batch_size, length = batch_equation.size()
        batch_equation_ = []
        for b in range(batch_size):
            equation = []
            for idx in range(length):
                equation.append(self.dataloader.dataset.out_symbol2idx[\
                                            self.dataloader.dataset.in_idx2word[\
                                                batch_equation[b,idx]]])
            batch_equation_.append(equation)
        batch_equation_ = torch.LongTensor(batch_equation_).to(self.config["device"])
        return batch_equation_

    def _train_batch(self, batch):
        outputs = self.model(batch["question"], batch["ques len"], batch["equation"])
        #outputs=torch.nn.functional.log_softmax(outputs,dim=1)
        if self.config["share_vocab"]:
            batch_equation = self._idx2word_2idx(batch["equation"])
            self.loss.eval_batch(outputs, batch_equation.view(-1))
        else:
            self.loss.eval_batch(outputs, batch["equation"].view(-1))
        batch_loss = self.loss.get_loss()
        return batch_loss

    def _eval_batch(self, batch):
        test_out = self.model(batch["question"], batch["ques len"])
        if self.config["share_vocab"]:
            target = self._idx2word_2idx(batch["equation"])
        else:
            target = batch["equation"]
        batch_size = target.size(0)
        val_acc = []
        equ_acc = []
        for idx in range(batch_size):
            val_ac, equ_ac, _, _ = self.evaluator.result_multi(test_out[idx], target[idx], batch["num list"][idx], batch["num stack"][idx])
            val_acc.append(val_ac)
            equ_acc.append(equ_ac)
        return val_acc, equ_acc

    def _train_epoch(self):
        epoch_start_time = time.time()
        loss_total = 0.
        self.model.train()
        for batch_idx, batch in enumerate(self.dataloader.load_data(DatasetType.Train)):
            self.batch_idx = batch_idx + 1
            self.model.zero_grad()
            batch_loss = self._train_batch(batch)
            loss_total += batch_loss
            self.loss.backward()
            self.optimizer.step()
            self.loss.reset()
        epoch_time_cost = time_since(time.time() - epoch_start_time)
        return loss_total, epoch_time_cost

    def fit(self):
        train_batch_size = self.config["train_batch_size"]
        epoch_nums = self.config["epoch_nums"]

        self.train_batch_nums = int(self.dataloader.trainset_nums / train_batch_size) + 1

        for epo in range(self.start_epoch, epoch_nums):
            self.epoch_i = epo + 1
            self.model.train()
            loss_total, train_time_cost = self._train_epoch()

            self.logger.info("epoch [%3d] avr loss [%2.8f] | train time %s"\
                                %(self.epoch_i,loss_total/self.train_batch_nums,train_time_cost))

            if epo % 2 == 0 or epo > epoch_nums - 5:
                valid_equ_ac, valid_val_ac, valid_total, valid_time_cost = self.evaluate(DatasetType.Valid)

                self.logger.info("---------- valid total [%d] | valid equ acc [%2.3f] | valid value acc [%2.3f] | valid time %s"\
                                %(valid_total,valid_equ_ac,valid_val_ac,valid_time_cost))
                test_equ_ac, test_val_ac, test_total, test_time_cost = self.evaluate(DatasetType.Test)

                self.logger.info("---------- test total [%d] | test equ acc [%2.3f] | test value acc [%2.3f] | test time %s"\
                                %(test_total,test_equ_ac,test_val_ac,test_time_cost))

                if valid_val_ac >= self.best_valid_value_accuracy:
                    self.best_valid_value_accuracy = valid_val_ac
                    self.best_valid_equ_accuracy = valid_equ_ac
                    self.best_test_value_accuracy = test_val_ac
                    self.best_test_equ_accuracy = test_equ_ac
                    self._save_model()
            if epo % 5 == 0:
                self._save_checkpoint()
        self.logger.info('''training finished.
                            best valid result: equation accuracy [%2.3f] | value accuracy [%2.3f]
                            best test result : equation accuracy [%2.3f] | value accuracy [%2.3f]'''\
                            %(self.best_valid_equ_accuracy,self.best_valid_value_accuracy,\
                                self.best_test_equ_accuracyself.best_test_value_accuracy))
    def evaluate(self, eval_set):
        self.model.eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0
        test_start_time = time.time()

        for batch in self.dataloader.load_data(eval_set):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)
        test_time_cost = time_since(time.time() - test_start_time)
        return equation_ac / eval_total, value_ac / eval_total, eval_total, test_time_cost

    def test(self):
        self._load_model()
        self.model.eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0
        test_start_time = time.time()

        for batch in self.dataloader.load_data(DatasetType.Test):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)
        test_time_cost = time_since(time.time() - test_start_time)
        self.logger.info("test total [%d] | test equ acc [%2.3f] | test value acc [%2.3f] | test time %s"\
                                %(eval_total,equation_ac/eval_total,value_ac/eval_total,test_time_cost))


class GTSTrainer(AbstractTrainer):
    def __init__(self, config, model, dataloader, evaluator):
        super().__init__(config, model, dataloader, evaluator)
        self._build_optimizer()
        if config["resume"]:
            self._load_checkpoint()
        self.loss = MaskedCrossEntropyLoss()

    def _build_optimizer(self):
        # optimizer
        self.embedder_optimizer = torch.optim.Adam(self.model.embedder.parameters(), self.config["learning_rate"], weight_decay=self.config["weight_decay"])
        self.encoder_optimizer = torch.optim.Adam(self.model.encoder.parameters(), self.config["learning_rate"], weight_decay=self.config["weight_decay"])
        self.decoder_optimizer = torch.optim.Adam(self.model.parameters(), self.config["learning_rate"], weight_decay=self.config["weight_decay"])
        self.node_generater_optimizer = torch.optim.Adam(self.model.node_generater.parameters(), self.config["learning_rate"], weight_decay=self.config["weight_decay"])
        self.merge_optimizer = torch.optim.Adam(self.model.merge.parameters(), self.config["learning_rate"], weight_decay=self.config["weight_decay"])
        # scheduler
        self.embedder_scheduler = torch.optim.lr_scheduler.StepLR(self.embedder_optimizer, step_size=self.config["step_size"], gamma=0.5)
        self.encoder_scheduler = torch.optim.lr_scheduler.StepLR(self.encoder_optimizer, step_size=self.config["step_size"], gamma=0.5)
        self.decoder_scheduler = torch.optim.lr_scheduler.StepLR(self.decoder_optimizer, step_size=self.config["step_size"], gamma=0.5)
        self.node_generater_scheduler = torch.optim.lr_scheduler.StepLR(self.node_generater_optimizer, step_size=self.config["step_size"], gamma=0.5)
        self.merge_scheduler = torch.optim.lr_scheduler.StepLR(self.merge_optimizer, step_size=self.config["step_size"], gamma=0.5)

    def _save_checkpoint(self):
        check_pnt = {
            "model": self.model.state_dict(),
            "embedder_optimizer": self.embedder_optimizer.state_dict(),
            "encoder_optimizer": self.encoder_optimizer.state_dict(),
            "decoder_optimizer": self.decoder_optimizer.state_dict(),
            "generate_optimizer": self.node_generater_optimizer.state_dict(),
            "merge_optimizer": self.merge_optimizer.state_dict(),
            "embedder_scheduler": self.embedder_scheduler.state_dict(),
            "encoder_scheduler": self.encoder_scheduler.state_dict(),
            "decoder_optimizer": self.decoder_optimizer.state_dict(),
            "generate_scheduler": self.node_generater_scheduler.state_dict(),
            "merge_scheduler": self.merge_scheduler.state_dict(),
            "start_epoch": self.epoch_i,
            "best_valid_value_accuracy": self.best_valid_value_accuracy,
            "best_valid_equ_accuracy": self.best_valid_equ_accuracy,
            "best_test_value_accuracy": self.best_test_value_accuracy,
            "best_test_equ_accuracy": self.best_test_equ_accuracy
        }
        torch.save(check_pnt, self.config["checkpoint_path"])

    def _load_checkpoint(self):
        check_pnt = torch.load(self.config["checkpoint_path"], map_location=self.config["map_location"])
        # load parameter of model
        self.model.load_state_dict(check_pnt["model"])
        # load parameter of optimizer
        self.embedder_optimizer.load_state_dict(check_pnt["embedder_optimizer"])
        self.encoder_optimizer.load_state_dict(check_pnt["encoder_optimizer"])
        self.decoder_optimizer.load_state_dict(check_pnt["decoder_optimizer"])
        self.node_generater_optimizer.load_state_dict(check_pnt["generate_optimizer"])
        self.merge_optimizer.load_state_dict(check_pnt["merge_optimizer"])
        #load parameter of scheduler
        self.embedder_scheduler.load_state_dict(check_pnt["embedding_scheduler"])
        self.encoder_scheduler.load_state_dict(check_pnt["encoder_scheduler"])
        self.decoder_scheduler.load_state_dict(check_pnt["decoder_scheduler"])
        self.node_generater_scheduler.load_state_dict(check_pnt["generate_scheduler"])
        self.merge_scheduler.load_state_dict(check_pnt["merge_scheduler"])
        # other parameter
        self.start_epoch = check_pnt["start_epoch"]
        self.best_valid_value_accuracy = check_pnt["best_valid_value_accuracy"]
        self.best_valid_equ_accuracy = check_pnt["best_valid_equ_accuracy"]
        self.best_test_value_accuracy = check_pnt["best_test_value_accuracy"]
        self.best_test_equ_accuracy = check_pnt["best_test_equ_accuracy"]

    def _scheduler_step(self):
        self.embedder_scheduler.step()
        self.encoder_scheduler.step()
        self.decoder_scheduler.step()
        self.node_generater_scheduler.step()
        self.merge_scheduler.step()

    def _optimizer_step(self):
        self.embedder_optimizer.step()
        self.encoder_optimizer.step()
        self.decoder_optimizer.step()
        self.node_generater_optimizer.step()
        self.merge_optimizer.step()

    def _model_zero_grad(self):
        self.model.embedder.zero_grad()
        self.model.encoder.zero_grad()
        self.model.decoder.zero_grad()
        self.model.node_generater.zero_grad()
        self.model.merge.zero_grad()

    def _model_train(self):
        self.model.embedder.train()
        self.model.encoder.train()
        self.model.decoder.train()
        self.model.node_generater.train()
        self.model.merge.train()

    def _model_eval(self):
        self.model.embedder.eval()
        self.model.encoder.eval()
        self.model.decoder.eval()
        self.model.node_generater.eval()
        self.model.merge.eval()

    def _train_batch(self, batch):
        '''
        seq, seq_length, nums_stack, num_size, generate_nums, num_pos,\
                UNK_TOKEN,num_start,target=None, target_length=None,max_length=30,beam_size=5
        '''
        unk = self.dataloader.out_unk_token
        num_start = self.dataloader.dataset.num_start
        generate_nums = [self.dataloader.dataset.out_symbol2idx[symbol] for symbol in self.dataloader.dataset.generate_list]

        outputs=self.model(batch["question"],batch["ques len"],batch["num stack"],batch["num size"],\
                                generate_nums,batch["num pos"],num_start,batch["equation"],batch["equ len"],UNK_TOKEN=unk)
        self.loss.eval_batch(outputs, batch["equation"], batch["equ mask"])
        batch_loss = self.loss.get_loss()
        return batch_loss

    def _eval_batch(self, batch):
        num_start = self.dataloader.dataset.num_start
        generate_nums = [self.dataloader.dataset.out_symbol2idx[symbol] for symbol in self.dataloader.dataset.generate_list]
        test_out=self.model(batch["question"],batch["ques len"],batch["num stack"],batch["num size"],\
                                generate_nums,batch["num pos"],num_start)

        val_ac, equ_ac, _, _ = self.evaluator.result(test_out, batch["equation"].tolist()[0], batch["num list"][0], batch["num stack"][0])
        return [val_ac], [equ_ac]

    def _train_epoch(self):
        epoch_start_time = time.time()
        loss_total = 0.
        self._model_train()
        for batch_idx, batch in enumerate(self.dataloader.load_data(DatasetType.Train)):
            self.batch_idx = batch_idx + 1
            self._model_zero_grad()
            batch_loss = self._train_batch(batch)
            loss_total += batch_loss
            self.loss.backward()
            self._optimizer_step()
            self.loss.reset()
        epoch_time_cost = time_since(time.time() - epoch_start_time)
        return loss_total, epoch_time_cost
        #print("epoch [%2d]avr loss [%2.8f]"%(self.epoch_i,loss_total /self.batch_nums))
        #print("epoch train time {}".format(time_since(time.time() -epoch_start_time)))

    def fit(self):
        train_batch_size = self.config["train_batch_size"]
        epoch_nums = self.config["epoch_nums"]
        self.train_batch_nums = int(self.dataloader.trainset_nums / train_batch_size) + 1
        for epo in range(self.start_epoch, epoch_nums):
            self.epoch_i = epo + 1
            self.model.train()
            loss_total, train_time_cost = self._train_epoch()
            self._scheduler_step()
            
            self.logger.info("epoch [%3d] avr loss [%2.8f] | train time %s"\
                                %(self.epoch_i,loss_total/self.train_batch_nums,train_time_cost))

            if epo % 10 == 0 or epo > epoch_nums - 5:
                valid_equ_ac, valid_val_ac, valid_total, valid_time_cost = self.evaluate(DatasetType.Valid)

                self.logger.info("---------- valid total [%d] | valid equ acc [%2.3f] | valid value acc [%2.3f] | valid time %s"\
                                %(valid_total,valid_equ_ac,valid_val_ac,valid_time_cost))
                test_equ_ac, test_val_ac, test_total, test_time_cost = self.evaluate(DatasetType.Test)

                self.logger.info("---------- test total [%d] | test equ acc [%2.3f] | test value acc [%2.3f] | test time %s"\
                                %(test_total,test_equ_ac,test_val_ac,test_time_cost))

                if valid_val_ac >= self.best_valid_value_accuracy:
                    self.best_valid_value_accuracy = valid_val_ac
                    self.best_valid_equ_accuracy = valid_equ_ac
                    self.best_test_value_accuracy = test_val_ac
                    self.best_test_equ_accuracy = test_equ_ac
                    self._save_model()
            if epo % 5 == 0:
                self._save_checkpoint()

    def evaluate(self, eval_set):
        self._model_eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0
        test_start_time = time.time()
        for batch in self.dataloader.load_data(eval_set):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)

        test_time_cost = time_since(time.time() - test_start_time)
        return equation_ac / eval_total, value_ac / eval_total, eval_total, test_time_cost

    def test(self):
        self._load_model()
        self.model.eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0
        test_start_time = time.time()

        for batch in self.dataloader.load_data(DatasetType.Test):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)
        test_time_cost = time_since(time.time() - test_start_time)
        self.logger.info("test total [%d] | test equ acc [%2.3f] | test value acc [%2.3f] | test time %s"\
                                %(eval_total,equation_ac/eval_total,value_ac/eval_total,test_time_cost))

class TransformerTrainer(AbstractTrainer):
    def __init__(self, config, model, dataloader, evaluator):
        super().__init__(config, model, dataloader, evaluator)
        self._build_optimizer()
        if config["resume"]:
            self._load_checkpoint()
        self._build_loss(config["symbol_size"], self.dataloader.dataset.out_symbol2idx[PAD_TOKEN])

    def _build_optimizer(self):
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.config["learning_rate"])
        #self.scheduler=torch.optim.lr_scheduler.StepLR(self.optimizer,step_size=5,gamma=0.8)
        self.optimizer = WarmUpScheduler(optimizer, self.config["learning_rate"], self.config["embedding_size"], self.config["warmup_steps"])

    def _save_checkpoint(self):
        check_pnt = {
            "model": self.model.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "start_epoch": self.epoch_i,
            "best_valid_value_accuracy": self.best_valid_value_accuracy,
            "best_valid_equ_accuracy": self.best_valid_equ_accuracy,
            "best_test_value_accuracy": self.best_test_value_accuracy,
            "best_test_equ_accuracy": self.best_test_equ_accuracy
        }
        torch.save(check_pnt, self.config["checkpoint_path"])

    def _load_checkpoint(self):
        #check_pnt = torch.load(self.config["checkpoint_path"],map_location="cpu")
        check_pnt = torch.load(self.config["checkpoint_path"], map_location=self.config["map_location"])
        # load parameter of model
        self.model.load_state_dict(check_pnt["model"])
        # load parameter of optimizer
        self.optimizer.load_state_dict(check_pnt["optimizer"])
        # other parameter
        self.start_epoch = check_pnt["start_epoch"]
        self.best_valid_value_accuracy = check_pnt["best_valid_value_accuracy"]
        self.best_valid_equ_accuracy = check_pnt["best_valid_equ_accuracy"]
        self.best_test_value_accuracy = check_pnt["best_test_value_accuracy"]
        self.best_test_equ_accuracy = check_pnt["best_test_equ_accuracy"]

    def _build_loss(self, symbol_size, out_pad_token):
        weight = torch.ones(symbol_size).to(self.config["device"])
        pad = out_pad_token
        self.loss = NLLLoss(weight, pad)

    def _idx2word_2idx(self, batch_equation):
        batch_size, length = batch_equation.size()
        batch_equation_ = []
        for b in range(batch_size):
            equation = []
            for idx in range(length):
                equation.append(self.dataloader.dataset.out_symbol2idx[\
                                            self.dataloader.dataset.in_idx2word[\
                                                batch_equation[b,idx]]])
            batch_equation_.append(equation)
        batch_equation_ = torch.LongTensor(batch_equation_).to(self.config["device"])
        return batch_equation_

    def _train_batch(self, batch):
        outputs = self.model(batch["question"], batch["equation"])
        outputs = torch.nn.functional.log_softmax(outputs, dim=1)
        if self.config["share_vocab"]:
            batch_equation = self._idx2word_2idx(batch["equation"])
            self.loss.eval_batch(outputs, batch_equation.view(-1))
        else:
            self.loss.eval_batch(outputs, batch["equation"].view(-1))
        batch_loss = self.loss.get_loss()
        return batch_loss

    def _eval_batch(self, batch):
        test_out = self.model(batch["question"])
        if self.config["share_vocab"]:
            target = self._idx2word_2idx(batch["equation"])
        else:
            target = batch["equation"]
        batch_size = target.size(0)
        val_acc = []
        equ_acc = []
        for idx in range(batch_size):
            val_ac, equ_ac, _, _ = self.evaluator.result(test_out[idx], target[idx], batch["num list"][idx], batch["num stack"][idx])
            val_acc.append(val_ac)
            equ_acc.append(equ_ac)
        return val_acc, equ_acc

    def _train_epoch(self):
        epoch_start_time = time.time()
        loss_total = 0.
        self.model.train()
        for batch_idx, batch in enumerate(self.dataloader.load_data(DatasetType.Train)):
            self.batch_idx = batch_idx + 1
            self.model.zero_grad()
            batch_loss = self._train_batch(batch)
            loss_total += batch_loss
            self.loss.backward()
            self.optimizer.step()
            self.loss.reset()
        epoch_time_cost = time_since(time.time() - epoch_start_time)
        return loss_total, epoch_time_cost

    def fit(self):
        train_batch_size = self.config["train_batch_size"]
        epoch_nums = self.config["epoch_nums"]

        self.train_batch_nums = int(self.dataloader.trainset_nums / train_batch_size) + 1

        for epo in range(self.start_epoch, epoch_nums):
            self.epoch_i = epo + 1
            self.model.train()
            loss_total, train_time_cost = self._train_epoch()
            self.logger.info("epoch [%3d] avr loss [%2.8f] | train time %s"\
                                %(self.epoch_i,loss_total/self.train_batch_nums,train_time_cost)\
                                +"\n---------- lr [%1.8f]"%(self.optimizer.get_lr()[0]))

            if epo % 2 == 0 or epo > epoch_nums - 5:
                valid_equ_ac, valid_val_ac, valid_total, valid_time_cost = self.evaluate(DatasetType.Train)

                self.logger.info("---------- valid total [%d] | valid equ acc [%2.3f] | valid value acc [%2.3f] | valid time %s"\
                                %(valid_total,valid_equ_ac,valid_val_ac,valid_time_cost))
                test_equ_ac, test_val_ac, test_total, test_time_cost = self.evaluate(DatasetType.Test)

                self.logger.info("---------- test total [%d] | test equ acc [%2.3f] | test value acc [%2.3f] | test time %s"\
                                %(test_total,test_equ_ac,test_val_ac,test_time_cost))

                if valid_val_ac >= self.best_valid_value_accuracy:
                    self.best_valid_value_accuracy = valid_val_ac
                    self.best_valid_equ_accuracy = valid_equ_ac
                    self.best_test_value_accuracy = test_val_ac
                    self.best_test_equ_accuracy = test_equ_ac
                    self._save_model()
            if epo % 5 == 0:
                self._save_checkpoint()

    def evaluate(self, eval_set):
        self.model.eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0

        test_start_time = time.time()
        for batch in self.dataloader.load_data(eval_set):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)
        test_time_cost = time_since(time.time() - test_start_time)
        return equation_ac / eval_total, value_ac / eval_total, eval_total, test_time_cost

    def test(self):
        self._load_model()
        self.model.eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0
        test_start_time = time.time()

        for batch in self.dataloader.load_data(DatasetType.Test):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)
        test_time_cost = time_since(time.time() - test_start_time)
        self.logger.info("test total [%d] | test equ acc [%2.3f] | test value acc [%2.3f] | test time %s"\
                                %(eval_total,equation_ac/eval_total,value_ac/eval_total,test_time_cost))

class SeqGANTrainer(AbstractTrainer):
    def __init__(self, config, model, dataloader, evaluator):
        super().__init__(config, model, dataloader, evaluator)
        self._build_optimizer()
        if config["resume"]:
            self._load_checkpoint()
        self._build_loss(config["symbol_size"], self.dataloader.dataset.out_symbol2idx[PAD_TOKEN])

    def _build_optimizer(self):
        self.generator_optimizer=torch.optim.Adam(self.model.generator.parameters(),\
                                                    lr=self.config["learning_rate"])
        self.discriminator_optimizer=torch.optim.Adam(self.model.discriminator.parameters(),\
                                                    lr=self.config["learning_rate"])

    def _build_loss(self, symbol_size, out_pad_token):
        weight = torch.ones(symbol_size).to(self.config["device"])
        pad = out_pad_token
        self.nll_loss = NLLLoss(weight, pad)
        self.binary_loss = BinaryCrossEntropyLoss()

    def _save_checkpoint(self):
        check_pnt = {
            "model": self.model.state_dict(),
            "generator_optimizer": self.generator_optimizer.state_dict(),
            "discriminator_optimizer": self.discriminator_optimizer.state_dict(),
            "best_valid_value_accuracy": self.best_valid_value_accuracy,
            "best_valid_equ_accuracy": self.best_valid_equ_accuracy,
            "best_test_value_accuracy": self.best_test_value_accuracy,
            "best_test_equ_accuracy": self.best_test_equ_accuracy
        }
        torch.save(check_pnt, self.config["checkpoint_path"])

    def _load_checkpoint(self):
        #check_pnt = torch.load(self.config["checkpoint_path"],map_location="cpu")
        check_pnt = torch.load(self.config["checkpoint_path"], map_location=self.config["map_location"])
        # load parameter of model
        self.model.load_state_dict(check_pnt["model"])
        # load parameter of optimizer
        self.generator_optimizer.load_state_dict(check_pnt["generator_optimizer"])
        self.discriminator_optimizer.load_state_dict(check_pnt["discriminator_optimizer"])
        # other parameter
        self.start_epoch = check_pnt["start_epoch"]
        self.best_value_accuracy = check_pnt["value_acc"]
        self.best_equ_accuracy = check_pnt["equ_acc"]

    def train_generator(self):
        print("generator pretrain...")
        for epo in range(20):
            loss_total = 0.
            self.model.generator.train()
            self.model.discriminator.eval()
            for batch_idx, batch in enumerate(self.dataloader.load_data("train")):
                self.model.zero_grad()
                outputs = self.model.generator.pre_train(batch["question"], batch["ques len"], batch["equation"])
                #outputs=torch.nn.functional.log_softmax(outputs,dim=1)
                if self.config["share_vocab"]:
                    batch_equation = self._idx2word_2idx(batch["equation"])
                    self.nll_loss.eval_batch(outputs, batch_equation.view(-1))
                else:
                    self.nll_loss.eval_batch(outputs, batch["equation"].view(-1))
                batch_loss = self.nll_loss.get_loss()
                loss_total += batch_loss
                self.nll_loss.backward()
                self.generator_optimizer.step()
                self.nll_loss.reset()
            print("epoch [%2d] avr loss [%2.8f]" % (epo + 1, loss_total / self.train_batch_nums))

    def train_discriminator(self):
        print("discriminator pretrain...")
        for epo in range(20):
            loss_total = 0.
            self.model.generator.eval()
            self.model.discriminator.train()
            for batch_idx, batch in enumerate(self.dataloader.load_data("train")):
                self.model.zero_grad()
                output, _, _, _ = self.model.generator(batch["question"], batch["ques len"])
                pred_y = self.model.discriminator(output)
                label_y = torch.zeros_like(pred_y).to(self.config["device"])
                self.binary_loss.eval_batch(pred_y, label_y)

                if self.config["share_vocab"]:
                    batch_equation = self._idx2word_2idx(batch["equation"])
                else:
                    batch_equation = batch["equation"]
                pred_y = self.model.discriminator(batch_equation)
                label_y = torch.ones_like(pred_y).to(self.config["device"])
                self.binary_loss.eval_batch(pred_y, label_y)

                norm = self.config['l2_reg_lambda'] * (self.model.discriminator.W_O.weight.norm() + self.model.discriminator.W_O.bias.norm())
                self.binary_loss.add_norm(norm)
                loss_total += self.binary_loss.get_loss()
                self.binary_loss.backward()
                self.discriminator_optimizer.step()
                self.binary_loss.reset()
            print("epoch [%2d] avr loss [%2.8f]" % (epo + 1, loss_total / self.train_batch_nums))

    def get_reward(self, outputs, monte_carlo_outputs, token_logits):
        rewards = 0
        batch_size = outputs.size(0)
        steps = len(monte_carlo_outputs)
        for idx in range(steps):
            output = self.model.discriminator(monte_carlo_outputs[idx])
            reward = output.reshape(batch_size, -1).mean(dim=1)
            mask = outputs[:, idx] != self.config["out_pad_token"]
            reward = reward * token_logits[idx] * mask.float()
            mask_sum = mask.sum()
            if (mask_sum):
                rewards += reward.sum() / mask_sum
        return -rewards

    def _train_batch(self, batch):
        self.model.generator.train()
        self.model.discriminator.eval()
        outputs, _, monte_carlo_outputs, P = self.model.generator(batch["question"], batch["ques len"], batch["equation"])
        g_loss = self.get_reward(outputs, monte_carlo_outputs, P)

        self.model.generator.eval()
        self.model.discriminator.train()
        pred_y = self.model.discriminator(outputs)
        label_y = torch.zeros_like(pred_y).to(self.config["device"])
        self.binary_loss.eval_batch(pred_y, label_y)

        if self.config["share_vocab"]:
            batch_equation = self._idx2word_2idx(batch["equation"])
        else:
            batch_equation = batch["equation"]
        pred_y = self.model.discriminator(batch_equation)
        label_y = torch.ones_like(pred_y).to(self.config["device"])
        self.binary_loss.eval_batch(pred_y, label_y)

        norm = self.config['l2_reg_lambda'] * (self.model.discriminator.W_O.weight.norm() + self.model.discriminator.W_O.bias.norm())
        self.binary_loss.add_norm(norm)
        d_loss = self.binary_loss.get_loss()
        self.model.generator.train()
        self.model.discriminator.train()
        return g_loss, d_loss

    def _eval_batch(self, batch):
        test_out = self.model(batch["question"], batch["ques len"])
        if self.config["share_vocab"]:
            target = self._idx2word_2idx(batch["equation"])
        else:
            target = batch["equation"]
        batch_size = target.size(0)
        val_acc = []
        equ_acc = []
        for idx in range(batch_size):
            val_ac, equ_ac, _, _ = self.evaluator.result(test_out[idx], target[idx], batch["num list"][idx], batch["num stack"][idx])
            val_acc.append(val_ac)
            equ_acc.append(equ_ac)
        return val_acc, equ_acc

    def _train_epoch(self):
        epoch_start_time = time.time()
        g_loss_total = 0.
        d_loss_total = 0.
        self.model.train()
        for batch_idx, batch in enumerate(self.dataloader.load_data(DatasetType.Train)):
            self.batch_idx = batch_idx + 1
            self.model.zero_grad()
            g_batch_loss, d_batch_loss = self._train_batch(batch)
            g_loss_total += g_batch_loss
            g_batch_loss.backward()
            self.generator_optimizer.step()
            d_loss_total += d_batch_loss
            self.binary_loss.backward()
            self.discriminator_optimizer.step()
            self.binary_loss.reset()
        epoch_time_cost = time_since(time.time() - epoch_start_time)
        return g_loss_total, d_loss_total, epoch_time_cost

    def fit(self):
        train_batch_size = self.config["train_batch_size"]
        epoch_nums = self.config["epoch_nums"]

        self.train_batch_nums = int(self.dataloader.trainset_nums / train_batch_size) + 1
        # generator pretrain
        self.train_generator()
        # discriminator pretrain
        self.train_discriminator()
        # seqgan train
        for epo in range(self.start_epoch, epoch_nums):
            self.epoch_i = epo + 1
            self.model.train()
            g_loss_total, d_loss_total, train_time_cost = self._train_epoch()
            print("epoch [%2d] avr g_loss [%2.8f] avr d_loss [%2.8f]"%(self.epoch_i,g_loss_total/self.train_batch_nums,\
                                                                        d_loss_total/self.train_batch_nums))
            print("---------- train time {}".format(train_time_cost))
            if epo % 2 == 0 or epo > epoch_nums - 5:
                equation_ac, value_ac, eval_total, test_time_cost = self.evaluate()
                print("---------- test equ acc [%2.3f] | test value acc [%2.3f]" % (equation_ac, value_ac))
                print("---------- test time {}".format(test_time_cost))
                if value_ac >= self.best_value_accuracy:
                    self.best_value_accuracy = value_ac
                    self.best_equ_accuracy = equation_ac
                    self._save_model()
            if epo % 5 == 0:
                self._save_checkpoint()

    def evaluate(self, eval_set):
        self.model.eval()
        value_ac = 0
        equation_ac = 0
        eval_total = 0
        test_start_time = time.time()
        for batch in self.dataloader.load_data(eval_set):
            batch_val_ac, batch_equ_ac = self._eval_batch(batch)
            value_ac += batch_val_ac.count(True)
            equation_ac += batch_equ_ac.count(True)
            eval_total += len(batch_val_ac)
        test_time_cost = time_since(time.time() - test_start_time)
        return equation_ac / eval_total, value_ac / eval_total, eval_total, test_time_cost


class GPT2Trainer(TransformerTrainer):
    def __init__(self, config, model, dataloader, evaluator):
        super().__init__(config, model, dataloader, evaluator)
        self._build_loss(config["vocab_size"], self.dataloader.dataset.out_symbol2idx[PAD_TOKEN])

    def _train_batch(self, batch):
        outputs, target = self.model(batch["ques_source"], batch["equ_source"])
        outputs = torch.nn.functional.log_softmax(outputs, dim=1)

        self.loss.eval_batch(outputs, target.view(-1))
        batch_loss = self.loss.get_loss()
        return batch_loss

    def _eval_batch(self, batch):
        test_out, _ = self.model(batch["ques_source"])
        target = batch["equ_source"]
        batch_size = len(target)
        val_acc = []
        equ_acc = []
        for idx in range(batch_size):
            val_ac, equ_ac, _, _ = self.evaluator.eval_source(test_out[idx], target[idx], batch["num list"][idx], batch["num stack"][idx])
            val_acc.append(val_ac)
            equ_acc.append(equ_ac)
        return val_acc, equ_acc
