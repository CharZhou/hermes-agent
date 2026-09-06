"""Image-specific endpoint and profile-scoped credential resolution."""

from agent.secret_scope import UnscopedSecretError, get_secret
from hermes_cli.providers import is_official_openai_host
from plugins.image_gen._common import load_image_gen_config


def resolve_endpoint() -> tuple[str | None, str | None]:
    config = load_image_gen_config("openai")
    raw_url = config.get("base_url")
    base_url = raw_url.strip().rstrip("/") or None if isinstance(raw_url, str) else None
    official = base_url is None or is_official_openai_host(base_url)
    key_env = next((value.strip() for field in ("key_env", "api_key_env")
                    if isinstance(value := config.get(field), str) and value.strip()), None)
    if not official and not key_env:
        return base_url, None

    def secret(name: str) -> str | None:
        try:
            value = get_secret(name)
        except UnscopedSecretError:
            return None
        return value.strip() if isinstance(value, str) and value.strip() else None

    api_key = secret(key_env) if key_env else None
    if not api_key and official:
        api_key = secret("OPENAI_API_KEY")
    return base_url, api_key


def credential_error(base_url: str | None) -> str:
    if base_url is not None and not is_official_openai_host(base_url):
        return (
            "Custom OpenAI image endpoint requires a non-empty `key_env` or "
            "`api_key_env` binding in `image_gen.openai`; plain API keys in "
            "config are not supported.")
    return (
        "OPENAI_API_KEY not set. Run `hermes tools` -> Image Generation -> "
        "OpenAI to configure, or `hermes setup` to add the key.")
