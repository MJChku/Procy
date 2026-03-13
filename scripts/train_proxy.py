#!/usr/bin/env python3
"""Train the procy proxy model (LoRA fine-tune) on evolve data.

The proxy learns: given (task + results history), generate the next
directional prompt — the kind of prompt a human expert would write
after looking at the results grid.

Training pairs:
  input:  task goal + results grid state
  output: the directional prompt that led to improvement

Usage (inside container, multi-GPU):
    pip install peft trl datasets accelerate
    python3 train_proxy.py --data /data/train.jsonl --output /data/proxy_lora

For 14B on 4x V100: loads in fp16 across GPUs via device_map="auto".
"""
import argparse
import json
import os
import sys

def build_sft_examples(data_path: str) -> list[dict]:
    """Convert evolve training data to SFT chat format.

    Each example: the proxy sees the task + history of tries with scores,
    and should output a directional prompt for the next iteration.
    """
    examples = []
    with open(data_path) as f:
        rows = [json.loads(line) for line in f if line.strip()]

    for row in rows:
        task = row.get("instruction", "")
        context = row.get("input", "{}")
        output = row.get("output", "")
        recall = row.get("recall", 0)

        if isinstance(context, str):
            try:
                context = json.loads(context)
            except json.JSONDecodeError:
                context = {}

        prev_best = context.get("previous_best_recall", 0)
        history = context.get("history", [])

        # Build the system + user message
        system_msg = (
            "You are a prompt optimization proxy. Given a task and the history of "
            "previous attempts with their scores, generate the next directional prompt "
            "that will improve results. Balance exploration (try new approaches) with "
            "exploitation (refine what works). Be specific and concise."
        )

        # Build history summary
        history_lines = []
        for h in history:
            history_lines.append(f"  {h['tag']}: recall@10={h['recall']:.4f}, {h['params']}")

        user_msg = f"Task: {task}\n"
        if history_lines:
            user_msg += f"\nPrevious attempts:\n" + "\n".join(history_lines) + "\n"
        user_msg += f"\nCurrent best recall@10: {prev_best:.4f}"
        user_msg += f"\nWhat should we try next?"

        # The output is what the human/expert would say
        # For now we use the code params as a proxy for the directional prompt
        # In real usage, this would be the human's actual words
        assistant_msg = f"Try: {output}" if len(output) < 200 else output[:200]

        examples.append({
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": assistant_msg},
            ]
        })

    return examples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="Training JSONL file")
    parser.add_argument("--model", default="Qwen/Qwen2.5-14B-Instruct",
                        help="Base model (default: Qwen 14B)")
    parser.add_argument("--output", default="/data/proxy_lora",
                        help="Output directory for LoRA adapter")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--gpus", type=int, default=None,
                        help="Number of GPUs (default: all available)")
    args = parser.parse_args()

    # Lazy imports
    import torch
    from datasets import Dataset
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from peft import LoraConfig, get_peft_model
    from trl import SFTConfig, SFTTrainer

    n_gpus = args.gpus or torch.cuda.device_count()
    print(f"CUDA available: {torch.cuda.is_available()}")
    print(f"GPUs available: {torch.cuda.device_count()}, using: {n_gpus}")
    for i in range(torch.cuda.device_count()):
        mem = torch.cuda.get_device_properties(i).total_memory / 1e9
        print(f"  GPU {i}: {torch.cuda.get_device_name(i)} ({mem:.0f} GB)")
    print(f"Model: {args.model}")
    print(f"Data: {args.data}")

    # Build training examples
    examples = build_sft_examples(args.data)
    print(f"Training examples: {len(examples)}")
    for i, ex in enumerate(examples[:5]):
        user = ex["messages"][1]["content"][:80]
        asst = ex["messages"][2]["content"][:60]
        print(f"  [{i}] user: {user}...")
        print(f"       asst: {asst}...")
    if len(examples) > 5:
        print(f"  ... and {len(examples) - 5} more")

    if len(examples) < 2:
        print("Need at least 2 examples. Duplicating with augmentation...")
        augmented = []
        for ex in examples:
            augmented.append(ex)
            aug = json.loads(json.dumps(ex))
            aug["messages"][1]["content"] = aug["messages"][1]["content"].replace(
                "What should we try next?", "Suggest the next optimization direction."
            )
            augmented.append(aug)
        examples = augmented

    dataset = Dataset.from_list(examples)

    print("Loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Multi-GPU: load in fp16 spread across GPUs via device_map="auto"
    # 14B fp16 = ~28GB, across 4x V100 32GB = ~7GB per GPU, plenty of room
    # No quantization needed — simpler and avoids BnB multi-GPU issues
    if n_gpus > 1:
        # Limit visible GPUs if requested
        device_map = "auto"
        if args.gpus:
            os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in range(args.gpus))
        print(f"Loading model (fp16, device_map=auto across {n_gpus} GPUs)...")
    else:
        device_map = {"": 0}
        print("Loading model (fp16, single GPU)...")

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        torch_dtype=torch.float16,
        device_map=device_map,
        trust_remote_code=True,
    )

    # Enable gradient checkpointing to save VRAM for activations
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()

    # LoRA config
    lora_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )

    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # Training config — fp16=True works on V100 (bf16 does NOT)
    sft_kwargs = dict(
        output_dir=args.output,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=4,
        learning_rate=args.lr,
        fp16=True,
        bf16=False,
        logging_steps=1,
        save_strategy="no",
        warmup_ratio=0.1,
        gradient_checkpointing=True,
        ddp_find_unused_parameters=False,
    )
    import inspect
    sig = inspect.signature(SFTConfig)
    if "max_seq_length" in sig.parameters:
        sft_kwargs["max_seq_length"] = 1024
    training_args = SFTConfig(**sft_kwargs)

    # Format messages for training
    def format_messages(example):
        text = tokenizer.apply_chat_template(
            example["messages"], tokenize=False, add_generation_prompt=False
        )
        return {"text": text}

    dataset = dataset.map(format_messages)

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        processing_class=tokenizer,
    )

    print("Training...")
    trainer.train()

    print(f"Saving LoRA adapter to {args.output}")
    model.save_pretrained(args.output)
    tokenizer.save_pretrained(args.output)

    # Quick inference test on GPU 0
    print("\n--- Inference test ---")
    model.eval()
    test_prompt = examples[0]["messages"][:2]  # system + user
    inputs = tokenizer.apply_chat_template(
        test_prompt, tokenize=True, add_generation_prompt=True,
        return_tensors="pt"
    )
    # For multi-GPU model, find the device of the first parameter
    first_device = next(model.parameters()).device
    inputs = inputs.to(first_device)

    with torch.no_grad():
        outputs = model.generate(inputs, max_new_tokens=100, temperature=0.7, do_sample=True)
    generated = tokenizer.decode(outputs[0][inputs.shape[-1]:], skip_special_tokens=True)
    print(f"Prompt: {test_prompt[1]['content'][:100]}...")
    print(f"Generated: {generated}")
    print("\nDone!")


if __name__ == "__main__":
    main()
