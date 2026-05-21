"""OpenAI-compatible API client for generating passages from words."""

import json
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


class APIError(Exception):
    """Raised when the API call fails."""

    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class WordPassageAPI:
    """Client for OpenAI-compatible chat completions API."""

    def __init__(self, config):
        self.endpoint = config["api_endpoint"].rstrip("/")
        if not self.endpoint.startswith("https://"):
            import sys
            if sys.stderr:
                print("WARNING: API endpoint is not HTTPS — API key may be sent in plaintext", file=sys.stderr)
        self.api_key = config["api_key"]
        self.model = config["model"]
        self.temperature = config["temperature"]
        self.max_tokens = config["max_tokens"]
        self.extra_headers = config.get("extra_headers", {})

    def generate_passage(self, words, system_prompt=None):
        """Generate a short passage incorporating the given words.

        Args:
            words: List of word strings.
            system_prompt: Custom system prompt. Uses default if None.

        Returns:
            The generated passage text.

        Raises:
            APIError: On any API or network failure.
        """
        if system_prompt is None:
            system_prompt = (
                "You are a professional English teacher. Write a natural, "
                "engaging English passage (150-300 words) that incorporates "
                "all the given words. The passage should be interesting and "
                "easy to understand, helping with word memorization. After the "
                "passage, list each target word with its Chinese definition."
            )

        user_content = (
            f"Please write a passage incorporating all of these words:\n"
            f"{', '.join(words)}\n\n"
            f"Word count: {len(words)} words."
        )

        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        headers.update(self.extra_headers)

        req = Request(
            f"{self.endpoint}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
            with urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"]
        except HTTPError as e:
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                detail = str(e)
            raise APIError(
                f"API returned error {e.code}: {detail}",
                status_code=e.code,
                body=detail,
            )
        except URLError as e:
            raise APIError(f"Network error: {e.reason}")
        except (KeyError, json.JSONDecodeError) as e:
            raise APIError(f"Unexpected API response format: {e}")


def test_connection(config):
    """Test the API connection with a minimal request (max_tokens=1).

    Returns:
        (success: bool, message: str)
    """
    endpoint = config["api_endpoint"].rstrip("/")
    api_key = config["api_key"]
    model = config["model"]

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "user", "content": "hi"},
        ],
        "max_tokens": 1,
    }).encode("utf-8")

    req = Request(
        f"{endpoint}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices")
            if choices is None:
                return False, f"API 响应格式异常: {json.dumps(body, ensure_ascii=False)[:200]}"
            return True, "连接成功！API 工作正常。"
    except HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = str(e)
        return False, f"API 返回错误 (HTTP {e.code}): {detail[:300]}"
    except URLError as e:
        return False, f"网络错误: {e.reason}"
    except Exception as e:
        return False, f"连接失败: {e}"


def fetch_models(endpoint, api_key):
    """Fetch available model IDs from the API's /models endpoint.

    Returns:
        (success: bool, models: list[str] | message: str)
    """
    endpoint = endpoint.rstrip("/")

    req = Request(
        f"{endpoint}/models",
        headers={
            "Authorization": f"Bearer {api_key}",
        },
        method="GET",
    )

    try:
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            models = [m["id"] for m in body.get("data", [])]
            if not models:
                return False, "未找到可用模型。"
            return True, sorted(models)
    except HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = str(e)
        return False, f"API 返回错误 (HTTP {e.code}): {detail[:200]}"
    except URLError as e:
        return False, f"网络错误: {e.reason}"
    except Exception as e:
        return False, f"获取失败: {e}"
