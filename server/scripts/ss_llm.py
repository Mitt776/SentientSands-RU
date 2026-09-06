"""
ss_llm.py — LLM call layer for SentientSands.

Owns the call_llm() function and all supporting utilities:
  - sanitize_llm_text: normalise unicode/encodings in LLM output
  - robust_json_parse: lenient JSON parser for LLM-generated JSON
  - call_llm: model lookup, provider routing, graceful degradation,
               400/422 retry without unsupported params

Runtime state accessed via server_runtime:
  runtime.models_config, runtime.providers_config, runtime.current_model_key

Extracted from kenshi_llm_server.py — [Design: Pineaxe]
"""

import json
import logging
import os
import re
import time
import traceback
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

import requests
from server_runtime import runtime, set_player2_session_key


_COMPAT_LOGGER_NAME = "ss.model_compat"
_COMPAT_HANDLER_MARKER = "_ss_model_compat_handler"


def _compat_logger():
    logger = logging.getLogger(_COMPAT_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if any(getattr(handler, _COMPAT_HANDLER_MARKER, False) for handler in logger.handlers):
        return logger
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_dir = os.path.normpath(os.path.join(script_dir, "..", "logs"))
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(log_dir, "model_compat.log"),
            maxBytes=512 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        setattr(handler, _COMPAT_HANDLER_MARKER, True)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    except Exception:
        logger.propagate = True
    return logger


def _safe_endpoint(url):
    try:
        parsed = urlparse(str(url or ""))
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else str(url or "")
    except Exception:
        return ""


def _message_stats(messages):
    if not isinstance(messages, list):
        return {"message_count": 1, "message_roles": ["raw"], "prompt_chars": len(str(messages or ""))}
    roles = []
    chars = 0
    for msg in messages:
        if isinstance(msg, dict):
            roles.append(str(msg.get("role") or "unknown"))
            chars += len(str(msg.get("content") or ""))
        else:
            roles.append(type(msg).__name__)
            chars += len(str(msg or ""))
    return {"message_count": len(messages), "message_roles": roles, "prompt_chars": chars}


def _compact_error(text, limit=240):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


def _log_model_compat(level, event, **fields):
    try:
        payload = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
        }
        payload.update(fields)
        _compat_logger().log(level, json.dumps(payload, ensure_ascii=True, sort_keys=True))
    except Exception:
        pass


# ─── LLM OUTPUT UTILITIES ────────────────────────────────────────────────────

def sanitize_llm_text(text):
    if not text: return ""
    # Replace common unicode/smart characters that Kenshi's engine might choke on
    replacements = {
        '\u2018': "'", '\u2019': "'", # Smart single quotes
        '\u201c': '"', '\u201d': '"', # Smart double quotes
        '\u2013': '-', '\u2014': '-', # En/Em dashes
        '\u2026': '...',             # Ellipsis
        '\u00a0': ' ',                # Non-breaking space
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    
    # Standardize line endings
    text = text.replace('\r\n', '\n')
    text = text.replace('\\n', '\n')  # Catch literal escaped newlines
    text = text.replace('\\r', '')    # Catch literal escaped carriage returns
    return text


def robust_json_parse(text):
    """Attempt to parse JSON while handling common LLM formatting errors."""
    if not text: return None
    
    # 1. Basic cleaning
    text = text.strip()
    
    # 2. Extract content between first { and last }
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1:
        return None
    
    json_str = text[start:end+1]
    
    # 3. Remove trailing commas within arrays/objects using regex
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
    
    # 4. Filter out any single-line comments // or multi-line /* */
    json_str = re.sub(r'//.*?\n', '\n', json_str)
    json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
    
    try:
        # strict=False prevents crashes from unescaped LLM newlines
        return json.loads(json_str, strict=False)
    except Exception as eFirst:
        # 5. Attempt: Sanitize unescaped quotes in middle of strings
        try:
            sanitized = re.sub(r'(?<=[a-zA-Z0-9])"(?=[a-zA-Z0-9\s])', "'", json_str)
            return json.loads(sanitized, strict=False)
        except:
            logging.error(f"ROBUST_JSON_PARSE: Final failure on string: {json_str[:200]}...")
            return None

# ─── LLM CALL ───────────────────────────────────────────────────────────────

def call_llm(messages, max_tokens=2048, temperature=0.8):
    call_id = f"{int(time.time() * 1000):x}"
    model_entry = runtime.models_config.get(runtime.current_model_key)
    if not model_entry:
        logging.error(f"Model Error: {runtime.current_model_key} not configured.")
        _log_model_compat(
            logging.ERROR,
            "model_not_configured",
            call_id=call_id,
            model_key=runtime.current_model_key,
            **_message_stats(messages),
        )
        return None

    provider_name = model_entry.get("provider")
    provider_config = runtime.providers_config.get(provider_name)
    if not provider_config:
        logging.error(f"Provider Error: {provider_name} not configured.")
        _log_model_compat(
            logging.ERROR,
            "provider_not_configured",
            call_id=call_id,
            model_key=runtime.current_model_key,
            provider=provider_name,
            model=model_entry.get("model"),
            **_message_stats(messages),
        )
        return None

    api_key = provider_config.get("api_key")
    if provider_name == "player2" and runtime.player2_session_key:
        api_key = runtime.player2_session_key

    base_url = (provider_config.get("base_url") or "").rstrip("/")
    target_url = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Sentient Sands Mod",
        "HTTP-Referer": "https://github.com/harvicusdev-glitch/SentientSands"
    }

    # player2 specific header
    if provider_name == "player2":
        headers["player2-game-key"] = "019c93fc-7a93-7ac4-8c6e-df0fd09bec01"

    payload = {
        "model": model_entry["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # Optional generation params are model-config driven.
    # To add sampling params for a specific model, set them in models.json:
    #   "params": {"top_p": 0.9, "repetition_penalty": 1.1}
    # To block a param inherited from a base config:
    #   "disable_params": ["top_p", "repetition_penalty"]
    model_params = model_entry.get("params") or {}
    if isinstance(model_params, dict):
        for k, v in model_params.items():
            if v is None:
                continue
            payload[k] = v

    disabled_params = model_entry.get("disable_params") or []
    if isinstance(disabled_params, list):
        for k in disabled_params:
            payload.pop(str(k), None)

    base_meta = {
        "call_id": call_id,
        "provider": provider_name,
        "model_key": runtime.current_model_key,
        "model": model_entry.get("model"),
        "endpoint": _safe_endpoint(target_url),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "optional_params": sorted(
            k for k in payload.keys()
            if k not in ("model", "messages", "max_tokens", "temperature")
        ),
    }
    base_meta.update(_message_stats(messages))

    def _strip_optional_sampling(p: dict) -> list:
        removed = []
        for k in ("repetition_penalty", "top_p"):
            if k in p:
                p.pop(k, None)
                removed.append(k)
        return removed
    
    last_error = None
    for attempt in range(3):
        try:
            getattr(runtime, "debug_logger", logging).debug(f"LLM REQUEST [{provider_name}] to {target_url} (Payload omitted for security)")
            start_time = time.time()
            response = requests.post(target_url, headers=headers, json=payload, timeout=120)
            elapsed = time.time() - start_time
            
            if response.status_code == 200:
                data = response.json()
                choices = data.get('choices', [])
                if not choices:
                    logging.warning(f"API Success but empty choices: {data}")
                    _log_model_compat(
                        logging.WARNING,
                        "empty_choices",
                        **base_meta,
                        attempt=attempt + 1,
                        status_code=response.status_code,
                        elapsed_ms=int(elapsed * 1000),
                    )
                    return None
                    
                msg_obj = choices[0].get('message', {})
                content = msg_obj.get('content')
                
                # Check for alternative fields used by some providers (Thinking/Reasoning/Legacy)
                if content is None:
                    # Try reasoning_content (DeepSeek/Thinking style)
                    content = msg_obj.get('reasoning_content')
                
                if content is None:
                    # Try legacy 'text' field just in case
                    content = choices[0].get('text')

                finish_reason = choices[0].get("finish_reason")
                logging.info(f"API Success in {elapsed:.1f}s (Attempt {attempt+1})")
                
                if content is None:
                    logging.warning(f"API Success but no content found in message. Message body: {msg_obj}")
                    getattr(runtime, "debug_logger", logging).warning(f"EMPTY RESPONSE DETAIL: {data}")
                    _log_model_compat(
                        logging.WARNING,
                        "missing_content",
                        **base_meta,
                        attempt=attempt + 1,
                        status_code=response.status_code,
                        finish_reason=finish_reason,
                        elapsed_ms=int(elapsed * 1000),
                    )
                    # If we got a 200 but no text, return a placeholder instead of None to prevent crashes
                    return "... (Empty Response)"

                getattr(runtime, "debug_logger", logging).debug(f"RAW LLM response received (Length: {len(content) if content else 0})")
                raw_model_response = str(content)

                # Robust Reasoning Block Removal
                if "</thought>" in content:
                    content = content.split("</thought>")[-1]
                
                # Strip XML-like thought tags if they remain
                content = re.sub(r'<thought>.*?</thought>', '', content, flags=re.DOTALL | re.IGNORECASE)
                content = re.sub(r'<thought>.*', '', content, flags=re.DOTALL | re.IGNORECASE)

                # Strip internal reasoning prefixes
                if "\n\n" in content and ("thought" in runtime.current_model_key.lower() or content.strip().lower().startswith("thought:")):
                    parts = content.split("\n\n")
                    # Only strip if the first part looks like a thought
                    if "thought" in parts[0].lower() or "reasoning" in parts[0].lower():
                        content = "\n\n".join(parts[1:])

                if not content.strip():
                    _log_model_compat(
                        logging.WARNING,
                        "blank_content",
                        **base_meta,
                        attempt=attempt + 1,
                        status_code=response.status_code,
                        finish_reason=finish_reason,
                        elapsed_ms=int(elapsed * 1000),
                    )
                    return "..."
                _log_model_compat(
                    logging.INFO,
                    "success",
                    **base_meta,
                    attempt=attempt + 1,
                    status_code=response.status_code,
                    finish_reason=finish_reason,
                    elapsed_ms=int(elapsed * 1000),
                    response_chars=len(content),
                    raw_response=raw_model_response,
                )
                return sanitize_llm_text(content.strip())
            elif response.status_code == 401 and provider_name == "player2":
                last_error = f"API ERROR 401: Unauthorized - attempting local token refresh"
                logging.warning(f"Player2 token expired/invalid (401). Attempting re-auth...")
                _log_model_compat(
                    logging.WARNING,
                    "player2_unauthorized",
                    **base_meta,
                    attempt=attempt + 1,
                    status_code=response.status_code,
                    elapsed_ms=int(elapsed * 1000),
                )
                try:
                    auth_url = f"http://localhost:4315/v1/login/web/019c93fc-7a93-7ac4-8c6e-df0fd09bec01"
                    auth_resp = requests.post(auth_url, timeout=5)
                    if auth_resp.status_code == 200:
                        new_key = auth_resp.json().get("p2Key")
                        if new_key:
                            set_player2_session_key(new_key)
                            headers["Authorization"] = f"Bearer {runtime.player2_session_key}"
                            logging.info("Successfully refreshed Player2 token locally.")
                except Exception as e:
                    logging.error(f"Failed to refresh Player2 token: {e}")

                # Universal compatibility fallback: on first failure, retry once
                # without optional sampling params.
                if attempt == 0:
                    removed = _strip_optional_sampling(payload)
                    if removed:
                        base_meta["optional_params"] = sorted(
                            k for k in payload.keys()
                            if k not in ("model", "messages", "max_tokens", "temperature")
                        )
                        _log_model_compat(
                            logging.WARNING,
                            "retry_without_optional_params",
                            **base_meta,
                            attempt=attempt + 1,
                            removed_params=removed,
                        )
                        logging.warning(
                            "Retrying without optional params after 401: %s",
                            ", ".join(removed),
                        )
                        continue

                logging.error(f"Attempt {attempt+1} failed after {elapsed:.1f}s: {last_error}")
                if attempt < 2:
                    time.sleep(1)
            else:
                last_error = f"API ERROR {response.status_code}: {_compact_error(getattr(response, 'reason', 'HTTP error'))}"
                logging.error(f"Attempt {attempt+1} failed after {elapsed:.1f}s: {last_error}")
                _log_model_compat(
                    logging.WARNING,
                    "api_error",
                    **base_meta,
                    attempt=attempt + 1,
                    status_code=response.status_code,
                    reason=_compact_error(getattr(response, "reason", "")),
                    elapsed_ms=int(elapsed * 1000),
                )
                # Universal compatibility fallback: on first failure, retry once
                # without optional sampling params (for any model/provider).
                if attempt == 0:
                    removed = _strip_optional_sampling(payload)
                    if removed:
                        base_meta["optional_params"] = sorted(
                            k for k in payload.keys()
                            if k not in ("model", "messages", "max_tokens", "temperature")
                        )
                        _log_model_compat(
                            logging.WARNING,
                            "retry_without_optional_params",
                            **base_meta,
                            attempt=attempt + 1,
                            removed_params=removed,
                        )
                        logging.warning(
                            "Retrying without optional params after API error: %s",
                            ", ".join(removed),
                        )
                        continue
                elif attempt < 2:
                    time.sleep(1)

        except Exception as e:
            last_error = str(e)
            logging.error(f"Attempt {attempt+1} Exception: {e}")
            getattr(runtime, "debug_logger", logging).error(f"LLM EXCEPTION STACK (Attempt {attempt+1}):\n{traceback.format_exc()}")
            _log_model_compat(
                logging.WARNING,
                "exception",
                **base_meta,
                attempt=attempt + 1,
                exception_type=type(e).__name__,
                error=_compact_error(e),
            )
            # Universal compatibility fallback on first exception too.
            if attempt == 0:
                removed = _strip_optional_sampling(payload)
                if removed:
                    base_meta["optional_params"] = sorted(
                        k for k in payload.keys()
                        if k not in ("model", "messages", "max_tokens", "temperature")
                    )
                    _log_model_compat(
                        logging.WARNING,
                        "retry_without_optional_params",
                        **base_meta,
                        attempt=attempt + 1,
                        removed_params=removed,
                    )
                    logging.warning(
                        "Retrying without optional params after exception: %s",
                        ", ".join(removed),
                    )
                    continue
            if attempt < 2:
                time.sleep(1)
    
    _log_model_compat(
        logging.ERROR,
        "failed",
        **base_meta,
        error=_compact_error(last_error),
    )
    return None

# ─── PUBLIC API ──────────────────────────────────────────────────────────────

__all__ = [
    "sanitize_llm_text",
    "robust_json_parse",
    "call_llm",
]
