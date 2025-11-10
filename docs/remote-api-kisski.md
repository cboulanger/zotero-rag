# KISSKI Chat-AI API Reference

## Overview

KISSKI (Knowledge and Information Services for Scientific Computing Infrastructure) provides an OpenAI-compatible API for academic research at GWDG (Gesellschaft für wissenschaftliche Datenverarbeitung mbH Göttingen).

**Official Website:** https://kisski.gwdg.de/

## API Configuration

### Endpoint
```
https://chat-ai.academiccloud.de/v1
```

### Authentication
- **Method:** Bearer token authentication
- **Header:** `Authorization: Bearer <API_KEY>`
- **API Key:** Obtain from KISSKI portal, store in environment variable `KISSKI_API_KEY`

### Available Models

As of 2025-11-10, the following models are available:

| Model ID | Model Name | Context | Best For |
|----------|------------|---------|----------|
| `llama-3.3-70b-instruct` | Meta Llama 3.3 70B Instruct | 128k | General purpose, RAG, instruction following |
| `deepseek-r1` | DeepSeek R1 0528 | ~64k | Reasoning tasks, complex queries |
| `deepseek-r1-distill-llama-70b` | DeepSeek R1 Distill Llama 70B | ~64k | Reasoning with Llama backbone |
| `mistral-large-instruct` | Mistral Large Instruct | 128k | High quality, large context |
| `qwen3-235b-a22b` | Qwen 3 235B A22B 2507 | ~32k | Largest model, best quality |
| `qwen3-32b` | Qwen 3 32B | ~32k | Balanced size/quality |
| `qwen2.5-coder-32b-instruct` | Qwen 2.5 Coder 32B Instruct | ~32k | Code generation/understanding |
| `meta-llama-3.1-8b-instruct` | Meta Llama 3.1 8B Instruct | 128k | Fast inference, smaller model |
| `gemma-3-27b-it` | Gemma 3 27B Instruct | ~8k | Multimodal (text+image) |
| `qwen2.5-vl-72b-instruct` | Qwen 2.5 VL 72B Instruct | ~32k | Vision+language, multimodal |

**Popular Models:**
- **Recommended for RAG:** `llama-3.3-70b-instruct` (128k context, excellent instruction following)
- **Recommended for reasoning:** `deepseek-r1` or `deepseek-r1-distill-llama-70b` (chain-of-thought)
- **Best quality:** `qwen3-235b-a22b` (largest model, highest capability)
- **Fastest:** `meta-llama-3.1-8b-instruct` (smaller, quick responses)

**Note:** Model availability changes frequently. Use the test script to verify current models:
```bash
uv run python scripts/test_kisski_api.py
```

## API Endpoints

The service provides two main OpenAI-compatible endpoints:

### 1. Text Completion API
**Endpoint:** `/v1/completions`

Used for text generation and completion tasks.

**Example Request:**
```bash
curl -i -N -X POST \
  --url https://chat-ai.academiccloud.de/v1/completions \
  --header 'Accept: application/json' \
  --header 'Authorization: Bearer <API_KEY>' \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "intel-neural-chat-7b",
    "prompt": "San Francisco is a",
    "max_tokens": 7,
    "temperature": 0
  }'
```

### 2. Chat Completion API
**Endpoint:** `/v1/chat/completions`

Used for conversational interactions (system/user/assistant messages).

**Example Request:**
```bash
curl -i -N -X POST \
  --url https://chat-ai.academiccloud.de/v1/chat/completions \
  --header 'Accept: application/json' \
  --header 'Authorization: Bearer <API_KEY>' \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "intel-neural-chat-7b",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant"},
      {"role": "user", "content": "How tall is the Eiffel tower?"}
    ],
    "temperature": 0
  }'
```

## Python Integration

### Using OpenAI Library

The KISSKI API is fully compatible with the OpenAI Python library:

```python
from openai import OpenAI

# API configuration
api_key = '<API_KEY>'  # Get from environment: os.getenv("KISSKI_API_KEY")
base_url = "https://chat-ai.academiccloud.de/v1"
model = "intel-neural-chat-7b"  # Or any available model

# Initialize client
client = OpenAI(
    api_key=api_key,
    base_url=base_url
)

# Get response
chat_completion = client.chat.completions.create(
    messages=[
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "How tall is the Eiffel tower?"}
    ],
    model=model,
)

# Extract response text
response_text = chat_completion.choices[0].message.content
print(response_text)
```

### Async Support

For async operations (as used in our backend):

```python
from openai import AsyncOpenAI

async def generate_text():
    client = AsyncOpenAI(
        api_key=os.getenv("KISSKI_API_KEY"),
        base_url="https://chat-ai.academiccloud.de/v1"
    )

    response = await client.chat.completions.create(
        model="mixtral-8x7b-instruct",
        messages=[{"role": "user", "content": "Hello!"}],
        max_tokens=100,
        temperature=0.7
    )

    return response.choices[0].message.content
```

## Configuration in Zotero RAG

### Environment Variables

Add to your `.env` file:
```bash
KISSKI_API_KEY=your_api_key_here
```

### Hardware Presets

The following presets use KISSKI API:

1. **remote-kisski** (recommended for most users)
   - Embeddings: Local (sentence-transformers/all-MiniLM-L6-v2)
   - LLM: Remote KISSKI API
   - Memory: ~1GB (minimal local processing)

2. **windows-test** (for Windows integration testing)
   - Embeddings: Remote OpenAI API
   - LLM: Remote KISSKI API
   - Memory: ~0.5GB (everything remote)

### Model Selection Guidelines

| Use Case | Recommended Model | Reasoning |
|----------|-------------------|-----------|
| RAG Question Answering | `mixtral-8x7b-instruct` | Best balance of quality and context length |
| Fast responses | `intel-neural-chat-7b` | Fastest inference, good quality for simple queries |
| Complex reasoning | `qwen1.5-72b-chat` | Largest model, best reasoning capabilities |
| General purpose | `meta-llama-3-70b-instruct` | Well-balanced, instruction-tuned |

## API Limits and Best Practices

### Rate Limiting
- Exact limits not specified in documentation
- Implement exponential backoff for retries
- Monitor response headers for rate limit information

### Best Practices

1. **Error Handling**
   - Always wrap API calls in try-except blocks
   - Check for HTTP 429 (rate limit) and 503 (service unavailable)
   - Implement retry logic with exponential backoff

2. **Model Selection**
   - Use smaller models for testing (intel-neural-chat-7b)
   - Use larger models for production (mixtral-8x7b-instruct, qwen1.5-72b-chat)
   - Consider context length requirements for your use case

3. **Token Management**
   - Monitor token usage (prompt + completion)
   - Set appropriate `max_tokens` limits
   - Consider chunking long documents to fit context windows

4. **Temperature Settings**
   - Use 0.0 for deterministic outputs (testing, citations)
   - Use 0.7-1.0 for creative responses
   - Default: 0.7 (good balance)

## Troubleshooting

### Common Issues

1. **"Model Not Found" Error**
   - **Cause:** Incorrect model name or model no longer available
   - **Solution:** Check current model list at KISSKI portal, update preset configuration

2. **401 Unauthorized**
   - **Cause:** Invalid or missing API key
   - **Solution:** Verify `KISSKI_API_KEY` environment variable is set correctly

3. **Connection Timeout**
   - **Cause:** Network issues or service temporarily unavailable
   - **Solution:** Implement retry logic, check service status

4. **Rate Limit Exceeded**
   - **Cause:** Too many requests in short time
   - **Solution:** Implement exponential backoff, reduce request frequency

### Testing Connectivity

Use the provided test script:
```bash
uv run python scripts/test_kisski_api.py
```

This will:
- Verify API key is configured
- Test connectivity to the endpoint
- Try a simple chat completion
- Report any errors with diagnostic information

## Security Notes

1. **API Key Storage**
   - NEVER commit API keys to version control
   - Store in environment variables or `.env` file (gitignored)
   - Use different keys for development/production if possible

2. **Academic Use Only**
   - KISSKI API is for academic research purposes
   - Respect usage terms and conditions
   - Do not share API keys outside your research group

3. **Data Privacy**
   - Data sent to KISSKI may be logged for service monitoring
   - Do not send sensitive or confidential information
   - For sensitive data, consider local-only deployment

## Additional Resources

- **KISSKI Portal:** https://kisski.gwdg.de/
- **GWDG Services:** https://www.gwdg.de/
- **OpenAI API Docs:** https://platform.openai.com/docs/api-reference (for API format reference)

---

**Last Updated:** 2025-11-10
**API Version:** v1
**Model List Version:** 2024-05-02
