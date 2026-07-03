import json
import os
import shutil
import subprocess
import sys
import time
import openai
from typing import Optional

def _log_raw(provider: str, model: str, raw: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[LLM_RAW] ts={ts} provider={provider} model={model} chars={len(raw or '')} raw={raw!r}")


class AbstractAIProvider:
    def __init__(self, name: str):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        raise NotImplementedError

    @property
    def is_available(self) -> bool:
        raise NotImplementedError

class AIProvider(AbstractAIProvider):
    """Lazy AI provider with on-demand initialization."""

    def __init__(self, name: str, var_name: str, model_name: str, base_url: str):
        super().__init__(name)
        self._var_name = var_name
        self._model_name = model_name
        self._base_url = base_url
        self._client = None  # lazy initialization

    def _ensure_client(self):
        """Initialize client on first use."""
        if self._client is None:
            self._client = self._create_client()

    def _create_client(self) -> Optional[openai.OpenAI]:
        """Create OpenAI client from environment."""
        proxy_url = os.environ.get("GATEWAY_URL")
        if proxy_url:
            prefix = self._name.lower()
            base_url = f"{proxy_url.rstrip('/')}/{prefix}/"
            print(f"[lib_llm_ext.AIProvider._create_client] Connecting via proxy: {base_url}")
            return openai.OpenAI(
                    api_key="proxy",
                    base_url=base_url,
                    )
        if self._var_name in os.environ:
            if self._var_name == "OLLAMA_API_KEY":
                llm_server_local_url = os.environ.get("LLM_SERVER_LOCAL_URL")
                if llm_server_local_url:
                    self._base_url = llm_server_local_url.rstrip("/") + "/v1"
                elif not self._base_url.endswith("/v1"):
                    self._base_url = self._base_url.rstrip("/") + "/v1"

            return openai.OpenAI(api_key=os.environ.get(self._var_name), base_url=self._base_url)

        return None

    @property
    def is_available(self) -> bool:
        """Check if provider is configured (without initializing)."""
        return bool(os.environ.get("GATEWAY_URL")) or bool(os.environ.get(self._var_name))

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        """Send chat request, initializing client if needed."""
        self._ensure_client()

        if self._client is None:
            raise RuntimeError(f"{self.name} not configured (set {self._var_name})")

        content = content.replace(":-:-:-:", " ")
        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens,
                **kwargs
            )

            raw = response.choices[0].message.content or ""
            _log_raw(self._name, self._model_name, raw)
            return self._clean_text(raw)
        except Exception as e:
            print(f"[lib_llm_ext.AIProvider.chat] Exception while communicating with LLM: {e}")
            return ""

    def _clean_text(self, text: str) -> str:
        """Unescape special characters."""
        return text.replace("_quote_", '"').replace("_apostrophe_", "'")

class OpenRouterProvider(AIProvider):
    """OpenRouter provider with reasoning mode enabled (reasoning tokens excluded from the response)."""

    def _create_client(self) -> Optional[openai.OpenAI]:
        """Create OpenRouter client from environment."""
        proxy_url = os.environ.get("GATEWAY_URL")
        if proxy_url:
            base_url = f"{proxy_url.rstrip('/')}/openrouter/"
            print(f"[lib_llm_ext.OpenRouterProvider._create_client] Connecting via proxy: {base_url}")
            return openai.OpenAI(
                    api_key="proxy",
                    base_url=base_url,
                    )
        if self._var_name in os.environ:
            return openai.OpenAI(api_key=os.environ.get(self._var_name), base_url=self._base_url)

        return None

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        return super().chat(content, max_tokens, reasoning, extra_body={
            "reasoning": {
                "enabled": True,
                "max_tokens": 6000,
                "exclude": True,
            }
        }, **kwargs)

class AsiOneProvider(AIProvider):
    """Lazy AI provider with on-demand initialization."""

    def __init__(self, name: str, var_name: str, model_name: str, base_url: str):
        super().__init__(name, var_name, model_name, base_url)

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        """Send chat request, initializing client if needed."""
        self._ensure_client()

        if self._client is None:
            raise RuntimeError(f"{self.name} not configured (set {self._var_name})")

        sysmsg, usermsg = content.split(":-:-:-:")
        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=[{"role": "system", "content": sysmsg},
                          {"role": "user", "content": usermsg}],
                max_tokens=max_tokens,
                extra_body={
                    "enable_thinking": True,
                    "thinking_budget": 6000
                },
                **kwargs
            )

            raw = response.choices[0].message.content
            _log_raw(self._name, self._model_name, raw)
            resp = self._clean_text(raw)
            resp = resp.replace("</arg_value>", " ").replace("</tool_call>", " ").replace("<arg_value>", " ").replace("<tool_call>", " ")
            return resp
        except Exception as e:
            print(f"[lib_llm_ext.ASIOneProvider.chat] Exception while communicating with LLM: {e}")
            return ""


class OpenAIProvider(AIProvider):
    """OpenAI provider using the Responses API (reasoning models)."""

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        """Send chat request via the Responses API, initializing client if needed."""
        self._ensure_client()

        if self._client is None:
            raise RuntimeError(f"{self.name} not configured (set {self._var_name})")

        if ":-:-:-:" in content:
            sysmsg, usermsg = content.split(":-:-:-:", 1)
        else:
            sysmsg, usermsg = "", content
        try:
            response = self._client.responses.create(
                model=self._model_name,
                instructions=sysmsg,
                input=usermsg,
                max_output_tokens=max_tokens,
                reasoning={"effort": reasoning},
                **kwargs
            )

            raw = response.output_text
            _log_raw(self._name, self._model_name, raw)
            return self._clean_text(raw)
        except Exception as e:
            print(f"[lib_llm_ext.OpenAIProvider.chat] Exception while communicating with LLM: {e}")
            return ""


class OpenClawProvider(AIProvider):
    """OpenClaw Gateway provider using the local OpenAI-compatible chat endpoint."""

    def __init__(self, name: str = "OpenClaw"):
        super().__init__(
            name=name,
            var_name="OPENCLAW_GATEWAY_TOKEN",
            model_name=os.environ.get("OPENCLAW_MODEL", "openclaw/default"),
            base_url=os.environ.get("OPENCLAW_GATEWAY_BASE_URL", "http://127.0.0.1:18789/v1"),
        )

    def _create_client(self) -> Optional[openai.OpenAI]:
        token = os.environ.get(self._var_name)
        if not token:
            return None
        print(f"[lib_llm_ext.OpenClawProvider._create_client] Connecting to OpenClaw Gateway: {self._base_url}")
        return openai.OpenAI(api_key=token, base_url=self._base_url)

    @property
    def is_available(self) -> bool:
        return bool(os.environ.get(self._var_name))

    def _failure_response(self, summary: str, detail: str = "") -> str:
        """Return a user-visible MeTTa send action for backend failures."""
        detail = (detail or "").strip().replace(os.environ.get("OPENCLAW_GATEWAY_TOKEN", "<unset>"), "<redacted>")
        if len(detail) > 900:
            detail = detail[:900] + "..."
        text = "ProtomegaTron backend stalled technically, rather than completing the reasoning call. " + summary
        if detail:
            text += "\nDiagnostic: " + detail
        text += "\nZeroBot/OpenClaw should inspect logs or retry with a longer/health-checked backend call."
        return f"(send {json.dumps(text)})"

    def _chat_subprocess(self, messages, max_tokens: int) -> str:
        """Call OpenClaw from a child Python process.

        SWI-Prolog/Janus has repeatedly segfaulted around the embedded Python
        OpenAI SDK call path.  Keeping the HTTP client in a short-lived child
        process leaves Janus with only subprocess I/O and a plain string result.
        """
        session_user = os.environ.get("OPENCLAW_SESSION_USER", "omegaclaw-local")
        if os.environ.get("OPENCLAW_SESSION_PER_CALL", "0").lower() in {"1", "true", "yes", "on"}:
            # OmegaClaw already supplies its own prompt/history. Reusing one
            # Gateway `user` session caused OpenClaw context to balloon far
            # beyond the model window, making later calls slow or empty.
            session_user = f"{session_user}-{int(time.time() * 1000)}"
        payload = {
            "model": os.environ.get("OPENCLAW_MODEL", self._model_name),
            "user": session_user,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        child_code = r'''
import json, os, sys, urllib.request
base = os.environ.get("OPENCLAW_GATEWAY_BASE_URL", "http://127.0.0.1:18789/v1").rstrip("/")
token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
payload = json.loads(sys.stdin.read())
req = urllib.request.Request(
    base + "/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    },
    method="POST",
)
timeout = int(os.environ.get("OPENCLAW_HTTP_TIMEOUT", "180"))
with urllib.request.urlopen(req, timeout=timeout) as response:
    data = json.loads(response.read().decode("utf-8", errors="replace"))
content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
sys.stdout.write(content)
'''
        python_exe = os.environ.get("OPENCLAW_SUBPROCESS_PYTHON") or shutil.which("python3") or sys.executable
        timeout = int(os.environ.get("OPENCLAW_SUBPROCESS_TIMEOUT", "240"))
        print(
            f"[lib_llm_ext.OpenClawProvider._chat_subprocess] start python={python_exe} "
            f"messages={len(messages)} chars={sum(len(str(m.get('content', ''))) for m in messages)} "
            f"max_tokens={max_tokens} timeout={timeout}",
            flush=True,
        )
        try:
            completed = subprocess.run(
                [python_exe, "-c", child_code],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"[lib_llm_ext.OpenClawProvider._chat_subprocess] child timed out after {timeout}s", flush=True)
            return self._failure_response(f"The OpenClaw child process exceeded its {timeout}s timeout.")
        except Exception as e:
            print(f"[lib_llm_ext.OpenClawProvider._chat_subprocess] child launch failed: {e}", flush=True)
            return self._failure_response("The OpenClaw child process could not be launched.", str(e))
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            print(f"[lib_llm_ext.OpenClawProvider._chat_subprocess] child failed rc={completed.returncode}: {stderr[:1200]}", flush=True)
            return self._failure_response(f"The OpenClaw HTTP child exited with rc={completed.returncode}.", stderr)
        print(f"[lib_llm_ext.OpenClawProvider._chat_subprocess] child ok chars={len(completed.stdout or '')}", flush=True)
        if not (completed.stdout or "").strip():
            return self._failure_response("The OpenClaw gateway returned an empty assistant message.")
        return completed.stdout or ""

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        if ":-:-:-:" in content:
            sysmsg, usermsg = content.split(":-:-:-:", 1)
            messages = [{"role": "system", "content": sysmsg}, {"role": "user", "content": usermsg}]
        else:
            messages = [{"role": "user", "content": content}]

        if os.environ.get("OPENCLAW_SUBPROCESS", "0").lower() in {"1", "true", "yes", "on"}:
            raw = self._chat_subprocess(messages, max_tokens)
            _log_raw(self._name, os.environ.get("OPENCLAW_MODEL", self._model_name), raw)
            return self._clean_text(raw)

        self._ensure_client()

        if self._client is None:
            raise RuntimeError(f"{self.name} not configured (set {self._var_name})")

        try:
            session_user = os.environ.get("OPENCLAW_SESSION_USER", "omegaclaw-local")
            if os.environ.get("OPENCLAW_SESSION_PER_CALL", "0").lower() in {"1", "true", "yes", "on"}:
                session_user = f"{session_user}-{int(time.time() * 1000)}"
            response = self._client.chat.completions.create(
                model=os.environ.get("OPENCLAW_MODEL", self._model_name),
                user=session_user,
                messages=messages,
                max_tokens=max_tokens,
                **kwargs
            )
            raw = response.choices[0].message.content or ""
            _log_raw(self._name, os.environ.get("OPENCLAW_MODEL", self._model_name), raw)
            return self._clean_text(raw)
        except Exception as e:
            print(f"[lib_llm_ext.OpenClawProvider.chat] Exception while communicating with OpenClaw Gateway: {e}")
            return self._failure_response("The native OpenClaw SDK call raised an exception.", str(e))


class TestProvider(AbstractAIProvider):
    """Test provider for mocking LLM output"""

    def __init__(self):
        super().__init__("Test")
        self._mock = None
        self._controller_ip = os.environ.get("TEST_SERVER_IP")

    def _llm_mock(self):
        if not self._mock:
            from Autotests.mock.llm import LlmMockAgent, LLM_MOCK_PORT
            self._mock = LlmMockAgent((self._controller_ip, LLM_MOCK_PORT))
        return self._mock

    @property
    def is_available(self) -> bool:
        return self._controller_ip is not None

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        return self._llm_mock().chat(content)

# Provider registry - lazy, no initialization yet
_provider_registry = {}


def _register_provider(name: str, var_name: str, model_name: str, base_url: str):
    """Register a provider configuration (no instantiation yet)."""
    _register_provider_instance(AIProvider(name, var_name, model_name, base_url))

def _register_provider_instance(provider: AbstractAIProvider):
    """Register a pre-initialized provider configuration (no instantiation yet)."""
    _provider_registry[provider.name] = provider

def _get_provider(name: str) -> Optional[AIProvider]:
    """Get or create provider instance on demand."""
    return _provider_registry.get(name)


# Register all providers (cheap - just stores config)
_register_provider(name="ASICloud", var_name="ASI_API_KEY", model_name="minimax/minimax-m3", base_url="https://inference.asicloud.cudos.org/v1")
_register_provider(name="Anthropic", var_name="ANTHROPIC_API_KEY", model_name="claude-opus-4-6", base_url="https://api.anthropic.com/v1/")
_register_provider(name="Ollama-local", var_name="OLLAMA_API_KEY", model_name="qwen3.5:9b", base_url="http://localhost:11434/v1")
_register_provider_instance(AsiOneProvider(name="ASIOne", var_name="ASIONE_API_KEY", model_name="asi1-ultra", base_url="https://api.asi1.ai/v1"))
_register_provider_instance(OpenRouterProvider(name="OpenRouter", var_name="OPENROUTER_API_KEY", model_name="z-ai/glm-5.1", base_url="https://openrouter.ai/api/v1"))
_register_provider_instance(OpenRouterProvider(name="MiniMaxM3", var_name="OPENROUTER_API_KEY", model_name="minimax/minimax-m3", base_url="https://openrouter.ai/api/v1"))
_register_provider_instance(TestProvider())
_register_provider_instance(OpenAIProvider(name="OpenAI", var_name="OPENAI_API_KEY", model_name="gpt-5.4", base_url="https://api.openai.com/v1"))
_register_provider_instance(OpenClawProvider())


def callProvider(provider_name: str, content: str, max_tokens: int = 6000, reasoning: str = "medium") -> str:
    """Generic dispatcher for MeTTa."""
    provider = _get_provider(provider_name)
    if not provider or not provider.is_available:
        raise RuntimeError(f"Provider '{provider_name}' not available")
    return provider.chat(content=content, max_tokens=max_tokens, reasoning=reasoning)



_embedding_model = None

def initLocalEmbedding():
    model_name="intfloat/e5-large-v2"
    global _embedding_model
    os.environ["HF_HUB_OFFLINE"] = "1"
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(model_name)
    return _embedding_model

def useLocalEmbedding(atom):
    global _embedding_model
    if _embedding_model is None:
        raise RuntimeError("Call initLocalEmbedding() first.")
    return _embedding_model.encode(
        atom,
        normalize_embeddings=True
    ).tolist()
