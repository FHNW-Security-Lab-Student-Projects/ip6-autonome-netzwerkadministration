# Defining Model Tiers by Price

This tier → model mapping is consumed by `run_experiment_matrix.sh:43-48` (the MODELS array).

High price:

OpenAI: GPT-5.5
$5 / $30 per 1M

Claude Opus 4.8
$5 / $25 per 1M

------------------------

Mid price:

z-ai/glm-5.2
$0.93–$0.95 / $3.00 per 1M (billed rates during the experiment window, input drifted; see docs/model-pricing.md)

qwen/qwen3.7-max
$1.25 / $3.75 per 1M

--------------------------
Low price:

DeepSeek: DeepSeek V3.2
$0.2288 / $0.3432 per 1M

Mistral: Ministral 14B 2512
$0.20 / $0.20 per 1M


OpenRouter:

mistralai/ministral-14b-2512
deepseek/deepseek-v3.2
qwen/qwen3.7-max
z-ai/glm-5.2
anthropic/claude-opus-4.8
openai/gpt-5.5
