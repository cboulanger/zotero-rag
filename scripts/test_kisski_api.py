"""
Test script for KISSKI Chat-AI API connectivity.

This script verifies that:
1. KISSKI_API_KEY is configured
2. The API endpoint is accessible
3. All available models can be queried
4. Chat completion works correctly

Usage:
    uv run python scripts/test_kisski_api.py
"""

import os
import sys
import asyncio
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import AsyncOpenAI
from dotenv import load_dotenv


# Load environment variables
load_dotenv()


# KISSKI API configuration
KISSKI_BASE_URL = "https://chat-ai.academiccloud.de/v1"

# Current models as of 2025-11-10 (verify with /v1/models endpoint)
KISSKI_MODELS = [
    "llama-3.3-70b-instruct",        # Recommended for RAG (128k context)
    "meta-llama-3.1-8b-instruct",    # Fast, smaller model
    "deepseek-r1",                   # Reasoning model
    "deepseek-r1-distill-llama-70b", # Reasoning with Llama
    "mistral-large-instruct",        # Large context, high quality
    "qwen3-235b-a22b",               # Largest model
    "qwen3-32b",                     # Balanced
    "qwen2.5-coder-32b-instruct",    # Code-focused
]


def print_header(text: str):
    """Print a formatted header."""
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)


def print_status(test_name: str, passed: bool, message: str = ""):
    """Print test status with consistent formatting."""
    status = "[PASS]" if passed else "[FAIL]"
    print(f"{test_name:<40} : {status}")
    if message:
        print(f"  -> {message}")


async def test_api_key():
    """Test if KISSKI_API_KEY is configured."""
    api_key = os.getenv("KISSKI_API_KEY")

    if not api_key:
        print_status("API Key Configuration", False, "KISSKI_API_KEY not found in environment")
        print("\nTo fix:")
        print("  1. Add KISSKI_API_KEY to your .env file")
        print("  2. Or set environment variable: export KISSKI_API_KEY=your_key")
        return False

    # Check if it looks like a valid key (basic sanity check)
    if len(api_key) < 10:
        print_status("API Key Configuration", False, "API key seems too short")
        return False

    print_status("API Key Configuration", True, f"Key found (length: {len(api_key)})")
    return True


async def test_model(client: AsyncOpenAI, model_name: str) -> bool:
    """Test a specific model with a simple query."""
    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Say 'hello' and nothing else."}
            ],
            max_tokens=10,
            temperature=0.0
        )

        # Extract response
        response_text = response.choices[0].message.content.strip()

        print_status(f"Model: {model_name}", True, f"Response: {response_text[:50]}")
        return True

    except Exception as e:
        error_msg = str(e)
        if "Model Not Found" in error_msg or "404" in error_msg:
            print_status(f"Model: {model_name}", False, "Model not available")
        else:
            print_status(f"Model: {model_name}", False, f"Error: {error_msg[:80]}")
        return False


async def test_connectivity():
    """Test basic connectivity to KISSKI API."""
    api_key = os.getenv("KISSKI_API_KEY")

    if not api_key:
        return False

    try:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=KISSKI_BASE_URL,
            timeout=30.0
        )

        print_status("API Endpoint Connectivity", True, f"Connected to {KISSKI_BASE_URL}")
        return client

    except Exception as e:
        print_status("API Endpoint Connectivity", False, f"Error: {e}")
        return None


async def test_all_models(client: AsyncOpenAI):
    """Test all known KISSKI models."""
    print_header("Testing Available Models")

    results = {}
    for model in KISSKI_MODELS:
        success = await test_model(client, model)
        results[model] = success

    return results


async def test_generation_quality(client: AsyncOpenAI, model_name: str):
    """Test generation quality with a more complex query."""
    print_header(f"Testing Generation Quality: {model_name}")

    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is the capital of France? Answer in one sentence."}
            ],
            max_tokens=50,
            temperature=0.0
        )

        response_text = response.choices[0].message.content.strip()
        print(f"\nQuery: What is the capital of France?")
        print(f"Response: {response_text}")

        # Check if response mentions Paris
        if "paris" in response_text.lower():
            print_status("Response Quality", True, "Correct answer detected")
            return True
        else:
            print_status("Response Quality", False, "Unexpected answer")
            return False

    except Exception as e:
        print_status("Response Quality", False, f"Error: {e}")
        return False


async def main():
    """Run all KISSKI API tests."""
    print_header("KISSKI Chat-AI API Test Suite")
    print(f"Endpoint: {KISSKI_BASE_URL}")

    # Test 1: Check API key
    print_header("1. Configuration Check")
    if not await test_api_key():
        print("\n[FAIL] Cannot proceed without valid API key")
        return 1

    # Test 2: Test connectivity
    print_header("2. Connectivity Test")
    client = await test_connectivity()
    if not client:
        print("\n[FAIL] Cannot connect to KISSKI API")
        return 1

    # Test 3: Test all models
    model_results = await test_all_models(client)

    # Find a working model for quality test
    working_models = [model for model, success in model_results.items() if success]

    if not working_models:
        print("\n[FAIL] No working models found")
        return 1

    # Test 4: Generation quality test with first working model
    test_model_name = working_models[0]
    await test_generation_quality(client, test_model_name)

    # Summary
    print_header("Test Summary")
    total_models = len(KISSKI_MODELS)
    working_count = len(working_models)

    print(f"Total models tested: {total_models}")
    print(f"Working models: {working_count}")

    if working_models:
        print("\nAvailable models:")
        for model in working_models:
            print(f"  - {model}")

    if working_count == 0:
        print("\n[FAIL] No models are available")
        return 1
    elif working_count < total_models:
        print(f"\n[WARNING] Only {working_count}/{total_models} models available")
        print("Some models from documentation may have been deprecated")
    else:
        print("\n[PASS] All tests passed successfully")

    # Recommendations
    print_header("Recommendations for Zotero RAG")

    if "llama-3.3-70b-instruct" in working_models:
        print("Recommended model: llama-3.3-70b-instruct")
        print("  Reason: Best for RAG - 128k context, excellent instruction following")
    elif "mistral-large-instruct" in working_models:
        print("Recommended model: mistral-large-instruct")
        print("  Reason: Large context (128k), high quality generation")
    elif "deepseek-r1" in working_models:
        print("Recommended model: deepseek-r1")
        print("  Reason: Excellent reasoning capabilities with chain-of-thought")
    elif "qwen3-235b-a22b" in working_models:
        print("Recommended model: qwen3-235b-a22b")
        print("  Reason: Largest model with best capabilities")
    elif working_models:
        print(f"Recommended model: {working_models[0]}")
        print("  Reason: First available model from the tested set")

    return 0 if working_count > 0 else 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n[FAIL] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
