import argparse
from dataclasses import dataclass
import os
from typing import Optional, Dict, List, Any, Callable

import accelerate
import datasets
import dotenv
import evaluate
import numpy as np
import pandas as pd
import peft
import torch
import tqdm
import transformers
import huggingface_hub


def load_training_data(dataset_name: str) -> pd.DataFrame:
    if dataset_name == "semeval":
        return pd.read_csv('./data/semeval/semeval_train.csv')
    raise ValueError(f"Unknown dataset: {dataset_name}")

def get_validation_split(df: pd.DataFrame) -> pd.DataFrame:
    val_split = 0.2
    df = df.sample(frac=1, random_state=42).reset_index(drop=True)
    return df.iloc[int(len(df) * (1 - val_split)):]

def load_test_data(dataset_name: str) -> pd.DataFrame:
    if dataset_name == "semeval":
        return pd.read_csv('./data/semeval/semeval_test.csv')
    raise ValueError(f"Unknown dataset: {dataset_name}")

def save_predictions(predictions: List[Any], df: pd.DataFrame, save_path: str) -> None:
    if not os.path.exists(save_path):
        os.makedirs(save_path)
    df['predictions'] = predictions
    df.to_csv(save_path + "/predictions.csv", index=False)

def print_metrics(metrics: Dict[str, float]) -> None:
    for metric, value in metrics.items():
        print(f"{metric}: {value}")

def get_model_save_path(task, model_path_dir, model_name):
    if task == "stance-classification":
        model_path_name = model_path_dir + "/" + model_name.replace('/', '-') + "-stance-classification"
    elif task == "argument-classification":
        model_path_name = model_path_dir + "/" + model_name.replace('/', '-') + "-argument-detection"
    elif task == "topic-extraction":
        model_path_name = model_path_dir + "/" + model_name.replace('/', '-') + "-topic-extraction"
    else:
        raise ValueError("Task not found")
    return model_path_name

def load_system_message(task: str) -> str:
    if task == "stance-classification" or task == "argument-classification":
        with open('./models/wiba/system_message_arg.txt', 'r') as file:
            system_message = file.read()
    elif task == "topic-extraction":
        with open('./models/wiba/system_message_cte.txt', 'r') as file:
            system_message = file.read()
    else:
        raise ValueError("Task not found")
    return system_message

def to_message_format(text, label):
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": text},
    ]
    if label is not None:
        messages.append({"role": "assistant", "content": label})
    return messages

def activate_neftune(model, accelerator, neftune_noise_alpha):
    r"""
    Activates the neftune as presented in this code: https://github.com/neelsjain/NEFTune and paper:
    https://arxiv.org/abs/2310.05914
    """
    unwrapped_model = accelerator.unwrap_model(model)

    if transformers.trainer._is_peft_model(unwrapped_model):
        embeddings = unwrapped_model.base_model.model.get_input_embeddings()
    else:
        embeddings = unwrapped_model.get_input_embeddings()

    del unwrapped_model

    embeddings.neftune_noise_alpha = neftune_noise_alpha
    hook_handle = embeddings.register_forward_hook(transformers.trainer_utils.neftune_post_forward_hook)
    return model, hook_handle

def deactivate_neftune(model, accelerator, neftune_hook_handle):
    """
    Deactivates the neftune method. Make sure to call `_activate_neftune` first.
    """
    if not neftune_hook_handle:
        raise ValueError("Neftune is not activated make sure to call `trainer._activate_neftune()` first")

    unwrapped_model = accelerator.unwrap_model(model)

    if transformers.trainer._is_peft_model(unwrapped_model):
        embeddings = unwrapped_model.base_model.model.get_input_embeddings()
    else:
        embeddings = unwrapped_model.get_input_embeddings()

    neftune_hook_handle.remove()
    del embeddings.neftune_noise_alpha, unwrapped_model

class ChatTemplateTokenizer:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def create_input_sequence_for_generation(self, sample):
        messages = to_message_format(sample['text'], None)
        inputs = self.tokenizer.apply_chat_template(
            messages, 
            add_generation_prompt=True,
            truncation=True,
            max_length=2048,
            padding='max_length',
            return_token_type_ids=False, 
            return_tensors='pt',
            return_dict=True
        )
        inputs['input_ids'] = inputs['input_ids'].squeeze(0)
        inputs['attention_mask'] = inputs['attention_mask'].squeeze(0)
        return inputs
    
    def create_input_sequence_for_training(self, sample):
        messages = to_message_format(sample['text'], sample['labels'])
        
        inputs = self.tokenizer.apply_chat_template(
            messages, 
            truncation=True, 
            max_length=2048, 
            padding='max_length', 
            return_tensors="pt",
            return_dict=True
        )

        inputs['input_ids'] = inputs['input_ids'].squeeze(0)
        inputs['attention_mask'] = inputs['attention_mask'].squeeze(0)
        
        # Find the assistant's response start
        response_tokens = self.tokenizer.encode(sample['labels'], add_special_tokens=False)
        response_start = None
        
        # Find where the assistant's response starts in the tokenized input
        for i in range(len(inputs['input_ids']) - len(response_tokens)):
            if inputs['input_ids'][i:i+len(response_tokens)].tolist() == response_tokens:
                response_start = i
                break
        
        # Create labels tensor with -100s before the response
        labels = inputs['input_ids'].clone()
        labels[:response_start] = -100
        
        return {
            "input_ids": inputs['input_ids'],
            "attention_mask": inputs['attention_mask'],
            "labels": labels
        }

@dataclass
class ModelConfig:
    model_name: str
    task: str
    num_labels: Optional[int]
    device_map: Dict[str, int]
    system_message: str
    tokenizer: Optional[transformers.PreTrainedTokenizer] = None
    model: Optional[transformers.PreTrainedModel] = None

@dataclass
class DataConfig:
    dataset_name: str
    add_system_message: bool
    add_topic: bool
    id2labels: Dict[str, int]

class DataProcessor:
    def __init__(self, model_config: ModelConfig, data_config: DataConfig):
        self.model_config = model_config
        self.data_config = data_config
        
    def process_data(self, df: pd.DataFrame, train: bool = True) -> datasets.Dataset:
        """Process dataframe into a format suitable for model input"""
        if self.model_config.task == "stance-classification":
            df = self._process_stance_classification(df)
        elif self.model_config.task == "topic-extraction":
            df = self._process_topic_extraction(df, train=train)
        else:
            raise ValueError(f"Unknown task: {self.model_config.task}")
            
        dataset = datasets.Dataset.from_pandas(df)
        dataset = self._add_prompts(dataset)
        dataset = self._tokenize_dataset(dataset, train=train)
        if train:
            columns = ['input_ids', 'attention_mask', 'labels']
        else:
            columns = ['input_ids', 'attention_mask']
        dataset.set_format(type='torch', columns=columns)
        if train:
            dataset.shuffle(seed=42)
        return dataset

    def get_loader(self, dataset: datasets.Dataset, loader_kwargs={}) -> torch.utils.data.DataLoader:
        if 'labels' in dataset.column_names:
            cols = ['input_ids', 'attention_mask', 'labels']
        else:
            cols = ['input_ids', 'attention_mask']
        return torch.utils.data.DataLoader(
            dataset.select_columns(cols),
            **loader_kwargs
        )
    
    def _process_stance_classification(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.rename(columns={"Stance": "class", "Tweet": "text", "Target": "topic"})
        if 'class' in df.columns:
            df["class"] = df["class"].map(lambda l: {
                'AGAINST': 'Argument_against',
                'FAVOR': 'Argument_for',
                'NONE': 'NoArgument'
            }[l])
            df["class"] = df["class"].map(self.data_config.id2labels)
            cols = ['text', 'class', 'topic']
        else:
            cols = ['text', 'topic']
        return df[cols]
    
    def _process_topic_extraction(self, df: pd.DataFrame, train: bool = True) -> pd.DataFrame:
        df = df.rename(columns={"Target": "topic", "Tweet": "text"})
        if train:
            cols = ['text', 'topic']
        else:
            cols = ['text']
        return df[cols]
    
    def _add_prompts(self, dataset: datasets.Dataset) -> datasets.Dataset:
        if self.data_config.add_system_message:
            if self.model_config.task == "stance-classification":
                return dataset.map(
                    lambda examples: {
                        "text": [
                            f"[INST] <<SYS>>\n{self.model_config.system_message.strip()}\n<</SYS>>\n\n"
                            f"Topic: '{topic}' Text: '{text}' [/INST] "
                            for topic, text in zip(examples['topic'], examples['text'])
                        ]
                    },
                    batched=True
                )
            elif self.model_config.task == "topic-extraction":
                return dataset.map(
                    lambda examples: {
                        "text": [
                            f"[INST] <<SYS>>\n{self.model_config.system_message.strip()}\n<</SYS>>\n\n"\
                            + "Text: '" + prompt +"' [/INST] "
                            for prompt in examples['text']
                        ]
                    }, batched=True)
            else:
                raise ValueError("Task not found")
        else:
            if self.model_config.task == "stance-classification":
                return dataset.map(
                    lambda examples: {
                        "text": [
                            f"Topic: '{topic}' Text: '{text}'"
                            for topic, text in zip(examples['topic'], examples['text'])
                        ]
                    },
                    batched=True
                )
            else:
                return dataset.map(
                    lambda examples: {
                        "text": [
                            f"Text: '{text}'"
                            for text in examples['text']
                        ]
                    },
                    batched=True
                )
            
    def _tokenize_dataset(self, dataset: datasets.Dataset, train: bool = True) -> datasets.Dataset:
        tokenizer = ChatTemplateTokenizer(self.model_config.tokenizer)
        if self.model_config.task in ["stance-classification", "argument-classification"]:
            dataset = dataset.map(tokenizer.create_input_sequence_for_generation)
            if train:
                dataset = dataset.rename_column("class", "labels")
        elif self.model_config.task == "topic-extraction":
            if train:
                dataset = dataset.rename_column("topic", "labels")
                dataset = dataset.map(tokenizer.create_input_sequence_for_training)
            else:
                dataset = dataset.map(tokenizer.create_input_sequence_for_generation)
        return dataset

class ModelEvaluator:
    def __init__(self, model_config: ModelConfig):
        self.model_config = model_config
        self.metrics = self._setup_metrics()
    
    def _setup_metrics(self) -> Dict[str, Any]:
        if self.model_config.task in ["stance-classification", "argument-classification"]:
            return {
                'accuracy': evaluate.load("accuracy"),
                'f1': evaluate.load("f1"),
                'precision': evaluate.load("precision"),
                'recall': evaluate.load("recall")
            }
        elif self.model_config.task == "topic-extraction":
            return {
                'bertscore': evaluate.load("bertscore"),
                'bleu': evaluate.load("bleu")
            }
        raise ValueError(f"Unknown task: {self.model_config.task}")
    
    def evaluate(self, predictions: List[Any], references: List[Any]) -> Dict[str, float]:
        if self.model_config.task in ["stance-classification", "argument-classification"]:
            return self._evaluate_classification(predictions, references)
        return self._evaluate_generation(predictions, references)
    
    def _evaluate_classification(self, predictions: List[Any], references: List[Any]) -> Dict[str, float]:
        return {
            'accuracy': self.metrics['accuracy'].compute(predictions=predictions, references=references)['accuracy'],
            'f1_macro': self.metrics['f1'].compute(predictions=predictions, references=references, average='macro')['f1'],
            'precision': self.metrics['precision'].compute(predictions=predictions, references=references, average='macro')['precision'],
            'recall': self.metrics['recall'].compute(predictions=predictions, references=references, average='macro')['recall']
        }
    
    def _evaluate_generation(self, predictions: List[Any], references: List[Any]) -> Dict[str, float]:
        bertscore_results = self.metrics['bertscore'].compute(predictions=predictions, references=references, lang="en")
        bleu = self.metrics['bleu'].compute(predictions=predictions, references=[[ref] for ref in references])
        return {
            'bertscore_f1': sum(bertscore_results['f1']) / len(bertscore_results['f1']),
            'bleu': bleu['bleu']
        }

@dataclass
class TrainingConfig:
    num_epochs: int
    learning_rate: float = 3e-5
    weight_decay: float = 0.1
    max_grad_norm: float = 0.3
    grad_accum_steps: int = 8
    batch_size: int = 1
    eval_steps: int = 100
    warmup_steps: int = 500
    neftune_noise_alpha: float = 5

class ModelTrainer:
    def __init__(
        self, 
        model_config: ModelConfig,
        training_config: TrainingConfig
    ):
        self.model_config = model_config
        self.training_config = training_config
        self.accelerator = accelerate.Accelerator(
            mixed_precision='fp16',
            gradient_accumulation_steps=training_config.grad_accum_steps
        )

    def set_model_and_tokenizer(self, model, tokenizer) -> None:
        """Set model and tokenizer"""
        self.model_config.model = model
        self.model_config.tokenizer = tokenizer

    def prepare_for_training(self) -> None:
        """Prepare model for training with LoRA"""
        self.model_config.model.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False}
        )
        
        if self.model_config.task in ["stance-classification", "argument-classification"]:
            self.model_config.model = peft.prepare_model_for_kbit_training(self.model_config.model)
        
        # Setup LoRA
        modules = self._find_all_linear_names()
        lora_kwargs = {}
        if self.model_config.task in ["stance-classification", "argument-classification"]:
            lora_kwargs['task_type'] = "SEQ_CLS"
            lora_kwargs['modules_to_save'] = ['score']
        elif self.model_config.task == "topic-extraction":
            lora_kwargs['task_type'] = "CAUSAL_LM"

        lora_config = peft.LoraConfig(
            r=8,
            lora_alpha=32,
            target_modules=modules,
            lora_dropout=0.05,
            bias="none",
            **lora_kwargs
        )
        
        self.model_config.model = peft.get_peft_model(self.model_config.model, lora_config)

    def _find_all_linear_names(self) -> list:
        """Find all linear layer names in model"""
        cls = torch.nn.Linear
        lora_module_names = set()
        for name, module in self.model_config.model.named_modules():
            if isinstance(module, cls):
                names = name.split('.')
                lora_module_names.add(names[0] if len(names) == 1 else names[-1])
        if 'lm_head' in lora_module_names:
            lora_module_names.remove('lm_head')
        return list(lora_module_names)

    def train(
        self, 
        train_dataset, 
        eval_dataset, 
        model_save_path: str,
        evaluator: ModelEvaluator
    ) -> None:
        """Train the model"""
        # Setup training components
        optimizer = torch.optim.AdamW(
            self.model_config.model.parameters(),
            lr=self.training_config.learning_rate,
            weight_decay=self.training_config.weight_decay
        )
        
        scheduler = transformers.get_constant_schedule(optimizer)
        
        # Prepare dataloaders
        train_loader = torch.utils.data.DataLoader(
            train_dataset.select_columns(['input_ids', 'attention_mask', 'labels']),
            batch_size=self.training_config.batch_size,
            shuffle=True
        )
        eval_loader = torch.utils.data.DataLoader(
            eval_dataset.select_columns(['input_ids', 'attention_mask', 'labels']),
            batch_size=self.training_config.batch_size
        )
        
        # Prepare training components
        (
            self.model_config.model,
            optimizer,
            train_loader,
            eval_loader,
            scheduler
        ) = self.accelerator.prepare(
            self.model_config.model,
            optimizer,
            train_loader,
            eval_loader,
            scheduler
        )
        
        self._training_loop(
            train_loader,
            eval_loader,
            optimizer,
            scheduler,
            evaluator,
            model_save_path
        )

    def _training_loop(
        self,
        train_loader,
        eval_loader,
        optimizer,
        scheduler,
        evaluator,
        model_save_path
    ):
        """Main training loop"""
        best_eval_metric = float('inf')
        chosen_metric = "f1_macro" if self.model_config.task in [
            "stance-classification",
            "argument-classification"
        ] else "bertscore_f1"

        assert len(train_loader) >= self.training_config.eval_steps * self.training_config.grad_accum_steps, \
            "Not enough steps to evaluate"
        
        self.model_config.model, neftune_hook = activate_neftune(
            self.model_config.model,
            self.accelerator,
            self.training_config.neftune_noise_alpha
        )
        global_step = 0
        loss = float('inf')
        pbar = tqdm.tqdm(total=self.training_config.eval_steps * self.training_config.grad_accum_steps, desc=f"Training round, loss: {loss:.4f}")
        for epoch in range(self.training_config.num_epochs):
            self.model_config.model.train()
            for step, batch in enumerate(train_loader):
                outputs = self.model_config.model(**batch)
                loss = outputs.loss / self.training_config.grad_accum_steps
                self.accelerator.backward(loss)

                pbar.set_description(f"Training round, loss: {loss.item():.4e}")
                pbar.update(1)
                
                if (step + 1) % self.training_config.grad_accum_steps == 0:
                    optimizer.step()
                    optimizer.zero_grad()
                    scheduler.step()
                    global_step += 1
                    
                    # Evaluation
                    if global_step % self.training_config.eval_steps == 0:
                        deactivate_neftune(
                            self.model_config.model,
                            self.accelerator,
                            neftune_hook
                        )
                        metrics = self._validation_step(eval_loader, evaluator)
                        state_str = f"Step {step},"
                        for key, val in metrics.items():
                            state_str += f" {key.title()}: {val:.4f},"
                        print(state_str)
                        
                        # Save best model
                        if metrics[chosen_metric] < best_eval_metric:
                            best_eval_metric = metrics[chosen_metric]
                            self.model_config.model.save_pretrained(model_save_path)
                            self.model_config.tokenizer.save_pretrained(model_save_path)
                            
                        self.model_config.model, neftune_hook = activate_neftune(
                            self.model_config.model,
                            self.accelerator,
                            self.training_config.neftune_noise_alpha
                        )

                        pbar = tqdm.tqdm(total=self.training_config.eval_steps * self.training_config.grad_accum_steps, desc=f"Training round, loss: {loss:.4f}")

    def _validation_step(self, eval_loader, evaluator):
        """Run validation step"""
        self.model_config.model.eval()
        all_preds = []
        all_labels = []
        
        pbar = tqdm.tqdm(total=len(eval_loader), desc="Validation round")
        with torch.no_grad():
            for batch in eval_loader:
                pbar.update(1)
                preds = get_prediction(batch, self.model_config.task, self.model_config.model, self.model_config.tokenizer)
                all_preds.extend(preds)
                
                if self.model_config.task in ["stance-classification", "argument-classification"]:
                    all_labels.extend(batch["labels"].cpu().numpy())
                else:
                    all_labels.extend(self.model_config.tokenizer.batch_decode(
                        batch["labels"][batch['labels'] != -100].unsqueeze(0),
                        skip_special_tokens=True
                    ))
        
        return evaluator.evaluate(all_preds, all_labels)

def setup_model_and_tokenizer(task, num_labels, device_map, model_save_path=None, model_name=None, hf_token=None):
    """Initialize model and tokenizer based on config"""
    model_path = model_save_path if model_save_path else model_name
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        model_path, 
        token=hf_token
    )
    
    if task in ["stance-classification", "argument-classification"]:
        model = transformers.AutoModelForSequenceClassification.from_pretrained(
            model_path,
            num_labels=num_labels,
            torch_dtype=torch.float16,
            device_map=device_map,
            attn_implementation='flash_attention_2',
            token=hf_token
        )
    elif task == "topic-extraction":
        model = transformers.AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            device_map=device_map,
            attn_implementation='flash_attention_2',
            token=hf_token
        )
    
    tokenizer.padding_side = 'left'
    tokenizer.pad_token = tokenizer.eos_token
    # Setup tokens
    if task in ["stance-classification", "argument-classification"]:
        model.config.pad_token_id = tokenizer.pad_token_id
    elif task == "topic-extraction":
        model.generation_config.pad_token_id = tokenizer.pad_token_id
        
    return model, tokenizer

def get_prediction(inputs, task, model, tokenizer):
    """Get model predictions"""
    if task in ["stance-classification", "argument-classification"]:
        inputs = {k: v.to(model.device) for k, v in inputs.items()}
        output = model(**inputs)
        predicted_class = torch.argmax(output.logits, dim=1)
        return predicted_class.cpu().tolist()
    else:
        if 'labels' in inputs:
            prompt = {
                "input_ids": inputs["input_ids"][inputs['labels'] == -100].unsqueeze(0),
                "attention_mask": inputs["attention_mask"][inputs['labels'] == -100].unsqueeze(0),
            }
        else:
            prompt = {
                "input_ids": inputs["input_ids"],
                "attention_mask": inputs["attention_mask"],
            }
        prompt = {k: v.to(model.device) for k, v in prompt.items()}
        output = model.generate(**prompt, max_new_tokens=20)
        completion = tokenizer.decode(
            output[0][prompt['input_ids'].shape[1]:],
            skip_special_tokens=True
        )
        return [completion]

def get_predictions(task, df, model_path_name, hf_token, config):
    # Setup configurations
    model_config = ModelConfig(
        model_name=None,
        task=task,
        num_labels=2 if task == "argument-classification" else 3,
        device_map={"": 0},
        system_message=load_system_message(task)
    )
    
    data_config = DataConfig(
        dataset_name=None,
        add_system_message=config.add_system_message,
        add_topic=config.add_topic,
        id2labels={
            "NoArgument": 0,
            "Argument_for": 1,
            "Argument_against": 2
        }
    )
    
    # Initialize components
    model, tokenizer = setup_model_and_tokenizer(model_config.task, model_config.num_labels, model_config.device_map, model_save_path=model_path_name, hf_token=hf_token)
    model_config.model, model_config.tokenizer = model, tokenizer
    processor = DataProcessor(model_config, data_config)
    test_dataset = processor.process_data(df, train=False)
    
    predictions = []
    test_loader = processor.get_loader(test_dataset, loader_kwargs={"batch_size": 1})
    for inputs in tqdm.tqdm(test_loader, desc="Evaluating"):
        predictions.extend(get_prediction(inputs, task, model, tokenizer))
    
    return predictions

def argument_detection(data, config, model_path, token):
    if model_path is None:
        hf_repo_url = "armaniii/llama-3-8b-argument-detection"
        local_directory = "./models/wiba/llama-3-8b-argument-detection"

        huggingface_hub.snapshot_download(repo_id=hf_repo_url,local_dir=local_directory)

    # with open('./models/wiba/system_message_arg.txt', 'r') as file:
    #     system_message = file.read()

    # tokenizer = AutoTokenizer.from_pretrained(local_directory)

    # try:
    #     import peft
    # except ImportError:
    #     raise ImportError("PEFT is not installed. Please install it by running 'pip install peft'")

    # model = AutoModelForSequenceClassification.from_pretrained(local_directory, num_labels=2, torch_dtype=torch.float16, device_map="auto", token=HF_TOKEN)

    # model.eval()

    # # Using Pipeline
    # pipe = pipeline(task="text-classification", model=model, tokenizer=tokenizer, padding=True, truncation=True, device_map="auto", max_length=2048, torch_dtype=torch.float16)

    # data['sentence'] = data['text'].map(lambda x: f"[INST] <<SYS>>\n{system_message.strip()}\n<</SYS>>\n\nText: '{x}' [/INST] ")
    # inputs = data['sentence']
    # prompts_generator = (p for p in inputs)
    # results = []
    # for out in tqdm(pipe(prompts_generator,batch_size=1)):          
    #     if out['label'] == 'LABEL_1':
    #         results.append('Argument')
    #     else:
    #         results.append('NoArgument')
    results = get_predictions("argument-classification", data, model_path)

    model.to('cpu')
    del model
    del pipe
    torch.cuda.empty_cache()

    data['is_argument'] = results
    return data

def target_extraction(data, config, model_path, token):
    if model_path is None:
        model_path = "armaniii/llama-3-8b-claim-topic-extraction"

    
    # model = AutoModelForCausalLM.from_pretrained("armaniii/llama-3-8b-claim-topic-extraction",torch_dtype=torch.float16,device_map="auto",low_cpu_mem_usage = True, token=HF_TOKEN)
    # tokenizer = AutoTokenizer.from_pretrained("armaniii/llama-3-8b-claim-topic-extraction",use_fast = False)

    # tokenizer.pad_token_id = tokenizer.eos_token_id
    # tokenizer.pad_token_id= 18610
    # tokenizer.padding_side = "left"

    # with open('./models/wiba/system_message_cte.txt', 'r') as file:
    #     system_message = file.read()

    # # Using Pipeline
    # pipe = pipeline('text-generation', model=model, tokenizer=tokenizer,max_new_tokens=8,device_map="auto",torch_dtype=torch.float16,pad_token_id=128009)
    # data['sentence'] = data['text'].map(lambda x: f'<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{system_message.strip()}<|eot_id|><|start_header_id|>user<|end_header_id|>\n\n{x}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n')
    # inputs = data['sentence']
    # results = []
    # for input in tqdm(inputs, total=len(inputs), desc="Generating Topics"):          
    #     out = pipe(input,batch_size=4)
    #     result=out[0]['generated_text']
    #     result = result.split("assistant<|end_header_id|>\n\n")[1]
    #     results.append(result)
    print(f"Extracting topics using model: {model_path}")
    results = get_predictions("topic-extraction", data, model_path, token, config)

    data['topic'] = results
    return data

def stance_detection(data, config, model_path, token):
    if model_path is None:
        hf_repo_url = "armaniii/llama-stance-classification"
        local_directory = "./models/wiba/llama-stance-classification"
        tokenizer_local_directory = "./models/wiba/llama-3-8b-argument-detection"

        snapshot_download(repo_id=hf_repo_url,local_dir=local_directory)

    # with open('./models/wiba/system_message_arg.txt', 'r') as file:
    #     system_message = file.read()

    # with open(os.path.join(local_directory, 'adapter_config.json'), 'r') as file:
    #     adapter_config = json.load(file)

    # base_model = adapter_config['base_model_name_or_path']

    # tokenizer = AutoTokenizer.from_pretrained(base_model)
    # tokenizer.pad_token = tokenizer.eos_token
    # model = AutoModelForSequenceClassification.from_pretrained(local_directory,num_labels=3, torch_dtype=torch.float16,device_map="auto",token=HF_TOKEN)
    # model.eval()

    # # Using Pipeline
    # pipe = pipeline(task="text-classification", model=model, tokenizer=tokenizer,padding=True,truncation=True,device_map="auto",max_length=2048,torch_dtype=torch.float16)

    # data['sentence'] = data[['topic', 'text']].apply(lambda r: f"[INST] <<SYS>>\n{system_message.strip()}\n<</SYS>>\n\nTarget: '{r['topic']}' Text: '{r['text']}' [/INST] ", axis=1)
    # inputs = data['sentence']
    # results = []
    # for input in tqdm(inputs):          
    #     out = pipe(input,batch_size=1)
    #     if out['label'] == 'LABEL_0':
    #         results.append('No Argument')
    #     elif out['label'] == 'LABEL_1':
    #         results.append('Argument in Favor')
    #     else:
    #         results.append('Argument Against')
    print(f"Detecting stance using model: {model_path}")
    results = get_predictions("stance-classification", data, model_path, token, config)
    mapping = {
        0: 'No Argument',
        1: 'Argument in Favor',
        2: 'Argument Against'
    }
    results = [mapping[r] for r in results]
    data['stance'] = results

    return data

class Wiba:
    def __init__(self):
        pass

    def fit_transform(self, docs, config, argument_detection_path=None, topic_extraction_path=None, stance_classification_path=None):
        dotenv.load_dotenv()
        HF_TOKEN = os.environ['HF_TOKEN']

        # https://github.com/Armaniii/WIBA
        data = pd.DataFrame(docs, columns=['text'])
        
        if argument_detection_path is not None:
            data = argument_detection(data, config, argument_detection_path, token=HF_TOKEN)
        else:
            # just set all as arguments
            data['is_argument'] = 'Argument'

        
        data = target_extraction(data, config, topic_extraction_path, HF_TOKEN)
        data = stance_detection(data, config, stance_classification_path, HF_TOKEN)
        data = data[['text', 'is_argument', 'topic', 'stance']]

        docs = data['text'].to_list()
        doc_targets = data['topic'].to_list()
        doc_targets = [[t] if not isinstance(t, list) else t for t in doc_targets]
        self.all_targets = list(set(data['topic'].drop_duplicates()))
        target_to_idx = {target: idx for idx, target in enumerate(self.all_targets)}
        probs = np.zeros((len(docs), len(self.all_targets)))
        for idx, targets in enumerate(doc_targets):
            for target in targets:
                probs[idx, target_to_idx[target]] = 1
        polarity = np.zeros((len(docs), len(self.all_targets)))
        for idx, targets in enumerate(doc_targets):
            for target in targets:
                if data['stance'][idx] == 'Argument in Favor':
                    polarity[idx, target_to_idx[target]] = 1
                elif data['stance'][idx] == 'Argument Against':
                    polarity[idx, target_to_idx[target]] = -1
        return doc_targets, probs, polarity
    
    def get_target_info(self):
        return pd.DataFrame({'ngram': self.all_targets})

    

    