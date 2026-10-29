"""Explicit configured model changes; no discovery requests or implicit replay."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import uuid

from .agent import Agent
from .config import GENERATION_FIELDS, MODEL_CONFIG_FIELDS, redact, validate_model_change
from .context import prepare_context
from .json_budget import encoded_size
from .provider import Provider
from .session import MAX_SESSION, SessionStore, validate_history
from .settings import Settings, TRANSPORT_FIELDS, credential_snapshot, default_config_path, load_settings


class ModelSelection:
    def __init__(self, settings, options, environment, initial):
        settings = Settings(settings.path, copy.deepcopy(settings.data), settings.project_path, copy.deepcopy(settings.project_data))
        self._options, self._initial = copy.deepcopy(options), initial
        reload_path = None if options.get("no_config") else settings.path or default_config_path()
        self._reload_path = Path(os.path.abspath(reload_path)) if reload_path else None
        targets, catalog = {}, []
        for provider, entry in settings.data.get("providers", {}).items():
            for alias, model in entry.get("models", {}).items():
                key = provider + "/" + alias
                targets[key] = (provider, alias)
                catalog.append({"key": key, "provider": provider, "alias": alias, "id": model["id"]})
            if entry.get("model"):
                targets[provider] = (provider, entry["model"])
                identity = entry.get("models", {}).get(entry["model"], {}).get("id", entry["model"])
                catalog.append({"key": provider, "provider": provider, "alias": "default", "id": identity})
        catalog.insert(0, {"key": "@startup", "provider": initial.provider_name, "alias": "startup", "id": initial.model})
        self._snapshot = (settings, dict(environment), targets, catalog)

    def catalog(self):
        return redact(copy.deepcopy(self._snapshot[3]), self._initial.secrets)

    def refresh(self, original, environ=None):
        snapshot, config, version = self._snapshot, original.config, original.tools._conversation_version
        def fresh():
            original.tools.root.ensure_open()
            original._check_cancelled()
            if (original._running or original.config is not config or original.tools.config is not config
                    or config._runtime_credentials is not self._initial._runtime_credentials
                    or original.tools._conversation_version != version or self._snapshot is not snapshot):
                raise ValueError("model catalog refresh is stale; retry while idle")
        fresh()
        if self._reload_path is None:
            raise ValueError("model catalog refresh is disabled by --no-config; restart with a configuration")
        try:
            settings = load_settings(self._reload_path, required=bool(snapshot[0].path))
            fresh()
            if snapshot[0].project_path:
                settings = settings.with_project(snapshot[0].project_path)
            environment = dict(os.environ if environ is None else environ)
            names, secrets = credential_snapshot(settings.data, environment)
            candidate = ModelSelection(settings, self._options, environment, self._initial)
        except (OSError, ValueError, TypeError, RecursionError):
            raise ValueError("cannot refresh model catalog; check the original private configuration and credential limits locally (values withheld)") from None
        fresh()
        config._runtime_credentials.remember_many(secrets, names)
        self._snapshot = candidate._snapshot  # Publish settings, credentials and rows together.
        return {"models_refreshed": True, "models": self.catalog(), "active_model_changed": False,
                "network_contacted": False, "task_executed": False}

    def resolve(self, current, key):
        settings, environment, targets, _ = self._snapshot
        if key == "@startup":
            resolved = self._initial
        else:
            if key not in targets:
                raise ValueError("choose an exact configured provider/model alias or @startup; no remote discovery")
            provider, model = targets[key]
            options, environment = dict(self._options), dict(environment)
            options.update(provider=provider, model=model)
            other_provider = provider != self._initial.provider_name
            if other_provider:
                for name in (*GENERATION_FIELDS, *TRANSPORT_FIELDS, "base_url"):
                    options.pop(name, None)
                environment.pop("SPRIG_BASE_URL", None)
            resolved, _ = settings.resolve(options, environment, configured_auth_only=other_provider)
        values = {name: getattr(resolved, name) for name in MODEL_CONFIG_FIELDS}
        current._runtime_credentials.remember_many((*current.credential_secrets, *resolved.credential_secrets),
                                                   (*current.credential_env_names, *resolved.credential_env_names))
        sources = dict(current.setting_sources)
        for name in MODEL_CONFIG_FIELDS - {"secret_values", "credential_env_vars", "setting_sources"}:
            sources[name] = "model selection"
        values["setting_sources"] = tuple(sorted(sources.items()))
        result = replace(current, **values)
        object.__setattr__(result, "_runtime_credentials", current._runtime_credentials)
        validate_model_change(current, result)
        return result


class ModelSwitch:
    def __init__(self, selection, original, history, key, *, saving=False, provider_factory=Provider):
        if original._running or original.tools.config is not original.config:
            raise ValueError("wait for the current task to finish before selecting a model")
        original.tools.root.ensure_open()
        original._check_cancelled()
        self.original = original
        self.selection, self.catalog_snapshot = selection, getattr(selection, "_snapshot", None)
        self.version = original.tools._conversation_version
        self.config = selection.resolve(original.config, key)
        changing = MODEL_CONFIG_FIELDS - {"secret_values", "credential_env_vars", "setting_sources"}
        self.changed = any(getattr(original.config, name) != getattr(self.config, name) for name in changing)
        self.name = "model-" + uuid.uuid4().hex if saving and self.changed else None
        self.ready = False
        self.history, self.dropped = None, 0
        self.agent = None
        if not self.changed:
            return
        if history:
            encoded_size(history, max_bytes=MAX_SESSION, check_cancelled=original._check_cancelled)
            projected = validate_history(history)
            for row in projected:
                for name in ("reasoning_content", "native_state"):
                    if name in row:
                        self.dropped += 1
                        del row[name]
            self.history = validate_history(redact(projected, self.config.secrets))
        self.agent = Agent(self.config, provider_factory(self.config), original.tools, original.emit,
                           checkpoint=original.checkpoint, emit_delta=original.emit_delta, cancel_event=original.cancel_event,
                           receive_input=original.receive_input, on_request_ready=original.on_request_ready, on_event=original.on_event)
        self.agent.restore_references(original.reference_state())
        self.agent.fork_usage(original)
        self.agent.compact_context = original.compact_context

    def description(self, directory):
        source, target = self.original.config, self.config
        text = (f"切换模型：{source.provider_name or '(startup)'}/{source.model} → {target.provider_name or '(startup)'}/{target.model}\n"
                f"目标端点：{target.base_url}\n协议：{target.wire_api}\n上下文上限：{target.max_context_bytes} 字节\n"
                f"临时 HTTP 错误最多额外重试：{target.retry_attempts} 次（共享 {target.request_deadline} 秒期限）\n"
                f"鉴权头名：{target.api_key_header}；自定义头名：{', '.join(name for name, _ in target.headers)}；代理：{target.proxy}\n"
                "生成设置：" + json.dumps(target.generation_options(), ensure_ascii=False) + "\n"
                f"移除 {self.dropped} 项旧推理/原生协议元数据；保留对话文字和工具历史，不能沿用旧批准或验收。\n"
                "只切换本地配置，不请求模型；下一条任务或继续会把保留的对话发送到此目标。草稿保留。\n"
                "工具权限、路径排除、验收命令、工作区和存储策略保持；清除旧读取标识、摘要和 MCP 连接/结果，不重放操作。\n"
                "受管后台命令停止；LSP 进程停止并清除结果。已有副作用不会撤销，历史任务不会在目标模型下重启。\n"
                "切换到其他提供商只使用其明确配置的鉴权，不沿用启动提供商的环境备用密钥、端点、传输或生成覆盖。\n"
                + ("目标原生/推理元数据会按配置用于后续请求及已启用的本地保存；本地文件不加密。\n"
                   if target.wire_api != "chat_completions" or target.preserve_reasoning else "")
                + (f"保存新私有分支：{directory}/{self.name}.json；原会话文件不覆盖。\n" if self.name else "当前未启用会话保存；保留在内存，可另存新会话。\n"))
        return redact(text, self.config.secrets)

    def preflight(self, approve, directory):
        if not self.changed:
            return False
        self.ready = False
        if (self.original.tools.config is not self.original.config or self.original._running
                or self.original.tools._conversation_version != self.version
                or not self.agent.usage_source_current(self.original)
                or getattr(self.selection, "_snapshot", None) is not self.catalog_snapshot):
            raise ValueError("model selection proposal is stale; select again")
        if approve(self.description(directory)) is not True:
            return False
        if getattr(self.selection, "_snapshot", None) is not self.catalog_snapshot:
            raise ValueError("model catalog changed during confirmation; select again")
        self.original._check_cancelled()
        history = self.history or [{"role": "system", "content": self.agent._prompt()}]
        self.agent._refresh_instructions(history)
        _, budget = prepare_context(self.config, self.agent._request_messages(history, 1), self.agent.schemas,
                                    self.agent.compact_context, check_cancelled=self.original._check_cancelled)
        if not budget["within_budget"]:
            raise ValueError("retained conversation exceeds the selected model context budget; original model and draft retained")
        self.agent.references.observed = self.agent._prepared_reference_observations
        self.ready = True
        return True

    def commit(self, directory):
        ready, self.ready = self.ready, False
        if (not self.changed or not ready or self.original._running
                or self.original.tools._conversation_version != self.version
                or not self.agent.usage_source_current(self.original)
                or self.original.tools.config is not self.original.config
                or getattr(self.selection, "_snapshot", None) is not self.catalog_snapshot):
            raise ValueError("model change must pass explicit confirmation and local preflight first")
        self.original._check_cancelled()
        self.original.tools.root.ensure_open()
        store = None
        try:
            if self.name:
                store = SessionStore(directory, self.config)
                if self.history:
                    store.save_new(self.name, self.history, **self.agent.reference_save_options())
                else:
                    store.ensure_new(self.name)
            self.original.tools.rebind_model(self.config)
        except BaseException:
            if store is not None:
                store.close()
            raise
        return self.agent, self.history, store, self.name
