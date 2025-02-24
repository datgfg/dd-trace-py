import json
import os
import time
from typing import Any
from typing import Dict
from typing import Optional
from typing import Union

import ddtrace
from ddtrace import Span
from ddtrace import config
from ddtrace import patch
from ddtrace._trace.context import Context
from ddtrace.ext import SpanTypes
from ddtrace.internal import atexit
from ddtrace.internal import forksafe
from ddtrace.internal._rand import rand64bits
from ddtrace.internal.compat import ensure_text
from ddtrace.internal.logger import get_logger
from ddtrace.internal.remoteconfig.worker import remoteconfig_poller
from ddtrace.internal.service import Service
from ddtrace.internal.service import ServiceStatusError
from ddtrace.internal.telemetry import telemetry_writer
from ddtrace.internal.telemetry.constants import TELEMETRY_APM_PRODUCT
from ddtrace.internal.utils.formats import asbool
from ddtrace.llmobs._constants import ANNOTATIONS_CONTEXT_ID
from ddtrace.llmobs._constants import INPUT_DOCUMENTS
from ddtrace.llmobs._constants import INPUT_MESSAGES
from ddtrace.llmobs._constants import INPUT_PARAMETERS
from ddtrace.llmobs._constants import INPUT_PROMPT
from ddtrace.llmobs._constants import INPUT_VALUE
from ddtrace.llmobs._constants import METADATA
from ddtrace.llmobs._constants import METRICS
from ddtrace.llmobs._constants import ML_APP
from ddtrace.llmobs._constants import MODEL_NAME
from ddtrace.llmobs._constants import MODEL_PROVIDER
from ddtrace.llmobs._constants import OUTPUT_DOCUMENTS
from ddtrace.llmobs._constants import OUTPUT_MESSAGES
from ddtrace.llmobs._constants import OUTPUT_VALUE
from ddtrace.llmobs._constants import PARENT_ID_KEY
from ddtrace.llmobs._constants import PROPAGATED_PARENT_ID_KEY
from ddtrace.llmobs._constants import SESSION_ID
from ddtrace.llmobs._constants import SPAN_KIND
from ddtrace.llmobs._constants import SPAN_START_WHILE_DISABLED_WARNING
from ddtrace.llmobs._constants import TAGS
from ddtrace.llmobs._evaluators.runner import EvaluatorRunner
from ddtrace.llmobs._trace_processor import LLMObsTraceProcessor
from ddtrace.llmobs._utils import AnnotationContext
from ddtrace.llmobs._utils import _get_llmobs_parent_id
from ddtrace.llmobs._utils import _get_ml_app
from ddtrace.llmobs._utils import _get_session_id
from ddtrace.llmobs._utils import _inject_llmobs_parent_id
from ddtrace.llmobs._utils import safe_json
from ddtrace.llmobs._utils import validate_prompt
from ddtrace.llmobs._writer import LLMObsEvalMetricWriter
from ddtrace.llmobs._writer import LLMObsSpanWriter
from ddtrace.llmobs.utils import Documents
from ddtrace.llmobs.utils import ExportedLLMObsSpan
from ddtrace.llmobs.utils import Messages
from ddtrace.propagation.http import HTTPPropagator


log = get_logger(__name__)


SUPPORTED_LLMOBS_INTEGRATIONS = {
    "anthropic": "anthropic",
    "bedrock": "botocore",
    "openai": "openai",
    "langchain": "langchain",
    "google_generativeai": "google_generativeai",
}


class LLMObs(Service):
    _instance = None  # type: LLMObs
    enabled = False

    def __init__(self, tracer=None):
        super(LLMObs, self).__init__()
        self.tracer = tracer or ddtrace.tracer
        self._llmobs_span_writer = None

        self._llmobs_span_writer = LLMObsSpanWriter(
            is_agentless=config._llmobs_agentless_enabled,
            interval=float(os.getenv("_DD_LLMOBS_WRITER_INTERVAL", 1.0)),
            timeout=float(os.getenv("_DD_LLMOBS_WRITER_TIMEOUT", 5.0)),
        )

        self._llmobs_eval_metric_writer = LLMObsEvalMetricWriter(
            site=config._dd_site,
            api_key=config._dd_api_key,
            interval=float(os.getenv("_DD_LLMOBS_WRITER_INTERVAL", 1.0)),
            timeout=float(os.getenv("_DD_LLMOBS_WRITER_TIMEOUT", 5.0)),
        )

        self._evaluator_runner = EvaluatorRunner(
            interval=float(os.getenv("_DD_LLMOBS_EVALUATOR_INTERVAL", 1.0)),
            llmobs_service=self,
        )

        self._trace_processor = LLMObsTraceProcessor(self._llmobs_span_writer, self._evaluator_runner)
        forksafe.register(self._child_after_fork)

        self._annotations = []
        self._annotation_context_lock = forksafe.RLock()
        self.tracer.on_start_span(self._do_annotations)

    def _do_annotations(self, span):
        # get the current span context
        # only do the annotations if it matches the context
        if span.span_type != SpanTypes.LLM:  # do this check to avoid the warning log in `annotate`
            return
        current_context = self._instance.tracer.current_trace_context()
        current_context_id = current_context.get_baggage_item(ANNOTATIONS_CONTEXT_ID)
        with self._annotation_context_lock:
            for _, context_id, annotation_kwargs in self._instance._annotations:
                if current_context_id == context_id:
                    self.annotate(span, **annotation_kwargs)

    def _child_after_fork(self):
        self._llmobs_span_writer = self._llmobs_span_writer.recreate()
        self._llmobs_eval_metric_writer = self._llmobs_eval_metric_writer.recreate()
        self._evaluator_runner = self._evaluator_runner.recreate()
        self._trace_processor._span_writer = self._llmobs_span_writer
        self._trace_processor._evaluator_runner = self._evaluator_runner
        if self.enabled:
            self._start_service()

    def _start_service(self) -> None:
        tracer_filters = self.tracer._filters
        if not any(isinstance(tracer_filter, LLMObsTraceProcessor) for tracer_filter in tracer_filters):
            tracer_filters += [self._trace_processor]
            self.tracer.configure(settings={"FILTERS": tracer_filters})
        try:
            self._llmobs_span_writer.start()
            self._llmobs_eval_metric_writer.start()
        except ServiceStatusError:
            log.debug("Error starting LLMObs writers")

        try:
            self._evaluator_runner.start()
        except ServiceStatusError:
            log.debug("Error starting evaluator runner")

    def _stop_service(self) -> None:
        try:
            self._llmobs_span_writer.stop()
            self._llmobs_eval_metric_writer.stop()
        except ServiceStatusError:
            log.debug("Error stopping LLMObs writers")

        try:
            self._evaluator_runner.stop()
        except ServiceStatusError:
            log.debug("Error stopping evaluator runner")

        try:
            forksafe.unregister(self._child_after_fork)
            self.tracer.shutdown()
        except Exception:
            log.warning("Failed to shutdown tracer", exc_info=True)

    @classmethod
    def enable(
        cls,
        ml_app: Optional[str] = None,
        integrations_enabled: bool = True,
        agentless_enabled: bool = False,
        site: Optional[str] = None,
        api_key: Optional[str] = None,
        env: Optional[str] = None,
        service: Optional[str] = None,
        _tracer: Optional[ddtrace.Tracer] = None,
    ) -> None:
        """
        Enable LLM Observability tracing.

        :param str ml_app: The name of your ml application.
        :param bool integrations_enabled: Set to `true` to enable LLM integrations.
        :param bool agentless_enabled: Set to `true` to disable sending data that requires a Datadog Agent.
        :param str site: Your datadog site.
        :param str api_key: Your datadog api key.
        :param str env: Your environment name.
        :param str service: Your service name.
        """
        if cls.enabled:
            log.debug("%s already enabled", cls.__name__)
            return

        if os.getenv("DD_LLMOBS_ENABLED") and not asbool(os.getenv("DD_LLMOBS_ENABLED")):
            log.debug("LLMObs.enable() called when DD_LLMOBS_ENABLED is set to false or 0, not starting LLMObs service")
            return

        # grab required values for LLMObs
        config._dd_site = site or config._dd_site
        config._dd_api_key = api_key or config._dd_api_key
        config.env = env or config.env
        config.service = service or config.service
        if os.getenv("DD_LLMOBS_APP_NAME"):
            log.warning("`DD_LLMOBS_APP_NAME` is deprecated. Use `DD_LLMOBS_ML_APP` instead.")
            config._llmobs_ml_app = ml_app or os.getenv("DD_LLMOBS_APP_NAME")
        config._llmobs_ml_app = ml_app or config._llmobs_ml_app

        # validate required values for LLMObs
        if not config._llmobs_ml_app:
            raise ValueError(
                "DD_LLMOBS_ML_APP is required for sending LLMObs data. "
                "Ensure this configuration is set before running your application."
            )

        config._llmobs_agentless_enabled = agentless_enabled or config._llmobs_agentless_enabled
        if config._llmobs_agentless_enabled:
            # validate required values for agentless LLMObs
            if not config._dd_api_key:
                raise ValueError(
                    "DD_API_KEY is required for sending LLMObs data when agentless mode is enabled. "
                    "Ensure this configuration is set before running your application."
                )
            if not config._dd_site:
                raise ValueError(
                    "DD_SITE is required for sending LLMObs data when agentless mode is enabled. "
                    "Ensure this configuration is set before running your application."
                )
            if not os.getenv("DD_REMOTE_CONFIG_ENABLED"):
                config._remote_config_enabled = False
                log.debug("Remote configuration disabled because DD_LLMOBS_AGENTLESS_ENABLED is set to true.")
                remoteconfig_poller.disable()

            # Since the API key can be set programmatically and TelemetryWriter is already initialized by now,
            # we need to force telemetry to use agentless configuration
            telemetry_writer.enable_agentless_client(True)

        if integrations_enabled:
            cls._patch_integrations()

        # override the default _instance with a new tracer
        cls._instance = cls(tracer=_tracer)
        cls.enabled = True
        cls._instance.start()

        atexit.register(cls.disable)
        telemetry_writer.product_activated(TELEMETRY_APM_PRODUCT.LLMOBS, True)

        log.debug("%s enabled", cls.__name__)

    @classmethod
    def _integration_is_enabled(cls, integration: str) -> bool:
        if integration not in SUPPORTED_LLMOBS_INTEGRATIONS:
            return False
        return SUPPORTED_LLMOBS_INTEGRATIONS[integration] in ddtrace._monkey._get_patched_modules()

    @classmethod
    def disable(cls) -> None:
        if not cls.enabled:
            log.debug("%s not enabled", cls.__name__)
            return
        log.debug("Disabling %s", cls.__name__)
        atexit.unregister(cls.disable)

        cls.enabled = False
        cls._instance.stop()
        cls._instance.tracer.deregister_on_start_span(cls._instance._do_annotations)
        telemetry_writer.product_activated(TELEMETRY_APM_PRODUCT.LLMOBS, False)

        log.debug("%s disabled", cls.__name__)

    @classmethod
    def annotation_context(
        cls, tags: Optional[Dict[str, Any]] = None, prompt: Optional[dict] = None, name: Optional[str] = None
    ) -> AnnotationContext:
        """
        Sets specified attributes on all LLMObs spans created while the returned AnnotationContext is active.
        Annotations are applied in the order in which annotation contexts are entered.

        :param tags: Dictionary of JSON serializable key-value tag pairs to set or update on the LLMObs span
                     regarding the span's context.
        :param prompt: A dictionary that represents the prompt used for an LLM call in the following form:
                        `{"template": "...", "id": "...", "version": "...", "variables": {"variable_1": "...", ...}}`.
                        Can also be set using the `ddtrace.llmobs.utils.Prompt` constructor class.
                        This argument is only applicable to LLM spans.
        :param name: Set to override the span name for any spans annotated within the returned context.
        """
        # id to track an annotation for registering / de-registering
        annotation_id = rand64bits()

        def get_annotations_context_id():
            current_ctx = cls._instance.tracer.current_trace_context()
            # default the context id to the annotation id
            ctx_id = annotation_id
            if current_ctx is None:
                current_ctx = Context(is_remote=False)
                current_ctx.set_baggage_item(ANNOTATIONS_CONTEXT_ID, ctx_id)
                cls._instance.tracer.context_provider.activate(current_ctx)
            elif not current_ctx.get_baggage_item(ANNOTATIONS_CONTEXT_ID):
                current_ctx.set_baggage_item(ANNOTATIONS_CONTEXT_ID, ctx_id)
            else:
                ctx_id = current_ctx.get_baggage_item(ANNOTATIONS_CONTEXT_ID)
            return ctx_id

        def register_annotation():
            with cls._instance._annotation_context_lock:
                ctx_id = get_annotations_context_id()
                cls._instance._annotations.append(
                    (annotation_id, ctx_id, {"tags": tags, "prompt": prompt, "_name": name})
                )

        def deregister_annotation():
            with cls._instance._annotation_context_lock:
                for i, (key, _, _) in enumerate(cls._instance._annotations):
                    if key == annotation_id:
                        cls._instance._annotations.pop(i)
                        return
                else:
                    log.debug("Failed to pop annotation context")

        return AnnotationContext(register_annotation, deregister_annotation)

    @classmethod
    def flush(cls) -> None:
        """
        Flushes any remaining spans and evaluation metrics to the LLMObs backend.
        """
        if cls.enabled is False:
            log.warning("flushing when LLMObs is disabled. No spans or evaluation metrics will be sent.")
            return
        try:
            cls._instance._llmobs_span_writer.periodic()
            cls._instance._llmobs_eval_metric_writer.periodic()
        except Exception:
            log.warning("Failed to flush LLMObs spans and evaluation metrics.", exc_info=True)

    @staticmethod
    def _patch_integrations() -> None:
        """Patch LLM integrations."""
        patch(**{integration: True for integration in SUPPORTED_LLMOBS_INTEGRATIONS.values()})  # type: ignore[arg-type]
        log.debug("Patched LLM integrations: %s", list(SUPPORTED_LLMOBS_INTEGRATIONS.values()))

    @classmethod
    def export_span(cls, span: Optional[Span] = None) -> Optional[ExportedLLMObsSpan]:
        """Returns a simple representation of a span to export its span and trace IDs.
        If no span is provided, the current active LLMObs-type span will be used.
        """
        if span:
            try:
                if span.span_type != SpanTypes.LLM:
                    log.warning("Span must be an LLMObs-generated span.")
                    return None
                return ExportedLLMObsSpan(span_id=str(span.span_id), trace_id="{:x}".format(span.trace_id))
            except (TypeError, AttributeError):
                log.warning("Failed to export span. Span must be a valid Span object.")
                return None
        span = cls._instance.tracer.current_span()
        if span is None:
            log.warning("No span provided and no active LLMObs-generated span found.")
            return None
        if span.span_type != SpanTypes.LLM:
            log.warning("Span must be an LLMObs-generated span.")
            return None
        return ExportedLLMObsSpan(span_id=str(span.span_id), trace_id="{:x}".format(span.trace_id))

    def _start_span(
        self,
        operation_kind: str,
        name: Optional[str] = None,
        session_id: Optional[str] = None,
        model_name: Optional[str] = None,
        model_provider: Optional[str] = None,
        ml_app: Optional[str] = None,
    ) -> Span:
        if name is None:
            name = operation_kind
        span = self.tracer.trace(name, resource=operation_kind, span_type=SpanTypes.LLM)
        span.set_tag_str(SPAN_KIND, operation_kind)
        if model_name is not None:
            span.set_tag_str(MODEL_NAME, model_name)
        if model_provider is not None:
            span.set_tag_str(MODEL_PROVIDER, model_provider)
        session_id = session_id if session_id is not None else _get_session_id(span)
        if session_id is not None:
            span.set_tag_str(SESSION_ID, session_id)
        if ml_app is None:
            ml_app = _get_ml_app(span)
        span.set_tag_str(ML_APP, ml_app)
        if span.get_tag(PROPAGATED_PARENT_ID_KEY) is None:
            # For non-distributed traces or spans in the first service of a distributed trace,
            # The LLMObs parent ID tag is not set at span start time. We need to manually set the parent ID tag now
            # in these cases to avoid conflicting with the later propagated tags.
            parent_id = _get_llmobs_parent_id(span) or "undefined"
            span.set_tag_str(PARENT_ID_KEY, str(parent_id))
        return span

    @classmethod
    def llm(
        cls,
        model_name: Optional[str] = None,
        name: Optional[str] = None,
        model_provider: Optional[str] = None,
        session_id: Optional[str] = None,
        ml_app: Optional[str] = None,
    ) -> Span:
        """
        Trace an invocation call to an LLM where inputs and outputs are represented as text.

        :param str model_name: The name of the invoked LLM. If not provided, a default value of "custom" will be set.
        :param str name: The name of the traced operation. If not provided, a default value of "llm" will be set.
        :param str model_provider: The name of the invoked LLM provider (ex: openai, bedrock).
                                   If not provided, a default value of "custom" will be set.
        :param str session_id: The ID of the underlying user session. Required for tracking sessions.
        :param str ml_app: The name of the ML application that the agent is orchestrating. If not provided, the default
                           value will be set to the value of `DD_LLMOBS_ML_APP`.

        :returns: The Span object representing the traced operation.
        """
        if cls.enabled is False:
            log.warning(SPAN_START_WHILE_DISABLED_WARNING)
        if model_name is None:
            model_name = "custom"
        if model_provider is None:
            model_provider = "custom"
        return cls._instance._start_span(
            "llm", name, model_name=model_name, model_provider=model_provider, session_id=session_id, ml_app=ml_app
        )

    @classmethod
    def tool(cls, name: Optional[str] = None, session_id: Optional[str] = None, ml_app: Optional[str] = None) -> Span:
        """
        Trace a call to an external interface or API.

        :param str name: The name of the traced operation. If not provided, a default value of "tool" will be set.
        :param str session_id: The ID of the underlying user session. Required for tracking sessions.
        :param str ml_app: The name of the ML application that the agent is orchestrating. If not provided, the default
                           value will be set to the value of `DD_LLMOBS_ML_APP`.

        :returns: The Span object representing the traced operation.
        """
        if cls.enabled is False:
            log.warning(SPAN_START_WHILE_DISABLED_WARNING)
        return cls._instance._start_span("tool", name=name, session_id=session_id, ml_app=ml_app)

    @classmethod
    def task(cls, name: Optional[str] = None, session_id: Optional[str] = None, ml_app: Optional[str] = None) -> Span:
        """
        Trace a standalone non-LLM operation which does not involve an external request.

        :param str name: The name of the traced operation. If not provided, a default value of "task" will be set.
        :param str session_id: The ID of the underlying user session. Required for tracking sessions.
        :param str ml_app: The name of the ML application that the agent is orchestrating. If not provided, the default
                           value will be set to the value of `DD_LLMOBS_ML_APP`.

        :returns: The Span object representing the traced operation.
        """
        if cls.enabled is False:
            log.warning(SPAN_START_WHILE_DISABLED_WARNING)
        return cls._instance._start_span("task", name=name, session_id=session_id, ml_app=ml_app)

    @classmethod
    def agent(cls, name: Optional[str] = None, session_id: Optional[str] = None, ml_app: Optional[str] = None) -> Span:
        """
        Trace a dynamic workflow in which an embedded language model (agent) decides what sequence of actions to take.

        :param str name: The name of the traced operation. If not provided, a default value of "agent" will be set.
        :param str session_id: The ID of the underlying user session. Required for tracking sessions.
        :param str ml_app: The name of the ML application that the agent is orchestrating. If not provided, the default
                           value will be set to the value of `DD_LLMOBS_ML_APP`.

        :returns: The Span object representing the traced operation.
        """
        if cls.enabled is False:
            log.warning(SPAN_START_WHILE_DISABLED_WARNING)
        return cls._instance._start_span("agent", name=name, session_id=session_id, ml_app=ml_app)

    @classmethod
    def workflow(
        cls, name: Optional[str] = None, session_id: Optional[str] = None, ml_app: Optional[str] = None
    ) -> Span:
        """
        Trace a predefined or static sequence of operations.

        :param str name: The name of the traced operation. If not provided, a default value of "workflow" will be set.
        :param str session_id: The ID of the underlying user session. Required for tracking sessions.
        :param str ml_app: The name of the ML application that the agent is orchestrating. If not provided, the default
                           value will be set to the value of `DD_LLMOBS_ML_APP`.

        :returns: The Span object representing the traced operation.
        """
        if cls.enabled is False:
            log.warning(SPAN_START_WHILE_DISABLED_WARNING)
        return cls._instance._start_span("workflow", name=name, session_id=session_id, ml_app=ml_app)

    @classmethod
    def embedding(
        cls,
        model_name: Optional[str] = None,
        name: Optional[str] = None,
        model_provider: Optional[str] = None,
        session_id: Optional[str] = None,
        ml_app: Optional[str] = None,
    ) -> Span:
        """
        Trace a call to an embedding model or function to create an embedding.

        :param str model_name: The name of the invoked embedding model.
                               If not provided, a default value of "custom" will be set.
        :param str name: The name of the traced operation. If not provided, a default value of "embedding" will be set.
        :param str model_provider: The name of the invoked LLM provider (ex: openai, bedrock).
                                   If not provided, a default value of "custom" will be set.
        :param str session_id: The ID of the underlying user session. Required for tracking sessions.
        :param str ml_app: The name of the ML application that the agent is orchestrating. If not provided, the default
                           value will be set to the value of `DD_LLMOBS_ML_APP`.

        :returns: The Span object representing the traced operation.
        """
        if cls.enabled is False:
            log.warning(SPAN_START_WHILE_DISABLED_WARNING)
        if model_name is None:
            model_name = "custom"
        if model_provider is None:
            model_provider = "custom"
        return cls._instance._start_span(
            "embedding",
            name,
            model_name=model_name,
            model_provider=model_provider,
            session_id=session_id,
            ml_app=ml_app,
        )

    @classmethod
    def retrieval(
        cls, name: Optional[str] = None, session_id: Optional[str] = None, ml_app: Optional[str] = None
    ) -> Span:
        """
        Trace a vector search operation involving a list of documents being returned from an external knowledge base.

        :param str name: The name of the traced operation. If not provided, a default value of "workflow" will be set.
        :param str session_id: The ID of the underlying user session. Required for tracking sessions.
        :param str ml_app: The name of the ML application that the agent is orchestrating. If not provided, the default
                           value will be set to the value of `DD_LLMOBS_ML_APP`.

        :returns: The Span object representing the traced operation.
        """
        if cls.enabled is False:
            log.warning(SPAN_START_WHILE_DISABLED_WARNING)
        return cls._instance._start_span("retrieval", name=name, session_id=session_id, ml_app=ml_app)

    @classmethod
    def annotate(
        cls,
        span: Optional[Span] = None,
        parameters: Optional[Dict[str, Any]] = None,
        prompt: Optional[dict] = None,
        input_data: Optional[Any] = None,
        output_data: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, Any]] = None,
        _name: Optional[str] = None,
    ) -> None:
        """
        Sets parameters, inputs, outputs, tags, and metrics as provided for a given LLMObs span.
        Note that with the exception of tags, this method will override any existing values for the provided fields.

        :param Span span: Span to annotate. If no span is provided, the current active span will be used.
                          Must be an LLMObs-type span, i.e. generated by the LLMObs SDK.
        :param prompt: A dictionary that represents the prompt used for an LLM call in the following form:
                        `{"template": "...", "id": "...", "version": "...", "variables": {"variable_1": "...", ...}}`.
                        Can also be set using the `ddtrace.llmobs.utils.Prompt` constructor class.
                        This argument is only applicable to LLM spans.
        :param input_data: A single input string, dictionary, or a list of dictionaries based on the span kind:
                           - llm spans: accepts a string, or a dictionary of form {"content": "...", "role": "..."},
                                        or a list of dictionaries with the same signature.
                           - embedding spans: accepts a string, list of strings, or a dictionary of form
                                              {"text": "...", ...} or a list of dictionaries with the same signature.
                           - other: any JSON serializable type.
        :param output_data: A single output string, dictionary, or a list of dictionaries based on the span kind:
                           - llm spans: accepts a string, or a dictionary of form {"content": "...", "role": "..."},
                                        or a list of dictionaries with the same signature.
                           - retrieval spans: a dictionary containing any of the key value pairs
                                              {"name": str, "id": str, "text": str, "score": float},
                                              or a list of dictionaries with the same signature.
                           - other: any JSON serializable type.
        :param parameters: (DEPRECATED) Dictionary of JSON serializable key-value pairs to set as input parameters.
        :param metadata: Dictionary of JSON serializable key-value metadata pairs relevant to the input/output operation
                         described by the LLMObs span.
        :param tags: Dictionary of JSON serializable key-value tag pairs to set or update on the LLMObs span
                     regarding the span's context.
        :param metrics: Dictionary of JSON serializable key-value metric pairs,
                        such as `{prompt,completion,total}_tokens`.
        """
        if span is None:
            span = cls._instance.tracer.current_span()
            if span is None:
                log.warning("No span provided and no active LLMObs-generated span found.")
                return
        if span.span_type != SpanTypes.LLM:
            log.warning("Span must be an LLMObs-generated span.")
            return
        if span.finished:
            log.warning("Cannot annotate a finished span.")
            return
        if metadata is not None:
            cls._tag_metadata(span, metadata)
        if metrics is not None:
            cls._tag_metrics(span, metrics)
        if tags is not None:
            cls._tag_span_tags(span, tags)
        span_kind = span.get_tag(SPAN_KIND)
        if parameters is not None:
            log.warning("Setting parameters is deprecated, please set parameters and other metadata as tags instead.")
            cls._tag_params(span, parameters)
        if _name is not None:
            span.name = _name
        if prompt is not None:
            cls._tag_prompt(span, prompt)
        if not span_kind:
            log.debug("Span kind not specified, skipping annotation for input/output data")
            return
        if input_data is not None or output_data is not None:
            if span_kind == "llm":
                cls._tag_llm_io(span, input_messages=input_data, output_messages=output_data)
            elif span_kind == "embedding":
                cls._tag_embedding_io(span, input_documents=input_data, output_text=output_data)
            elif span_kind == "retrieval":
                cls._tag_retrieval_io(span, input_text=input_data, output_documents=output_data)
            else:
                cls._tag_text_io(span, input_value=input_data, output_value=output_data)

    @staticmethod
    def _tag_prompt(span, prompt: dict) -> None:
        """Tags a given LLMObs span with a prompt"""
        try:
            validated_prompt = validate_prompt(prompt)
            span.set_tag_str(INPUT_PROMPT, safe_json(validated_prompt))
        except TypeError:
            log.warning("Failed to validate prompt with error: ", exc_info=True)
            return

    @staticmethod
    def _tag_params(span: Span, params: Dict[str, Any]) -> None:
        """Tags input parameters for a given LLMObs span.
        Will be mapped to span's `meta.input.parameters` field.
        """
        if not isinstance(params, dict):
            log.warning("parameters must be a dictionary of key-value pairs.")
            return
        span.set_tag_str(INPUT_PARAMETERS, safe_json(params))

    @classmethod
    def _tag_llm_io(cls, span, input_messages=None, output_messages=None):
        """Tags input/output messages for LLM-kind spans.
        Will be mapped to span's `meta.{input,output}.messages` fields.
        """
        if input_messages is not None:
            try:
                if not isinstance(input_messages, Messages):
                    input_messages = Messages(input_messages)
                if input_messages.messages:
                    span.set_tag_str(INPUT_MESSAGES, safe_json(input_messages.messages))
            except TypeError:
                log.warning("Failed to parse input messages.", exc_info=True)
        if output_messages is None:
            return
        try:
            if not isinstance(output_messages, Messages):
                output_messages = Messages(output_messages)
            if not output_messages.messages:
                return
            span.set_tag_str(OUTPUT_MESSAGES, safe_json(output_messages.messages))
        except TypeError:
            log.warning("Failed to parse output messages.", exc_info=True)

    @classmethod
    def _tag_embedding_io(cls, span, input_documents=None, output_text=None):
        """Tags input documents and output text for embedding-kind spans.
        Will be mapped to span's `meta.{input,output}.text` fields.
        """
        if input_documents is not None:
            try:
                if not isinstance(input_documents, Documents):
                    input_documents = Documents(input_documents)
                if input_documents.documents:
                    span.set_tag_str(INPUT_DOCUMENTS, safe_json(input_documents.documents))
            except TypeError:
                log.warning("Failed to parse input documents.", exc_info=True)
        if output_text is None:
            return
        span.set_tag_str(OUTPUT_VALUE, safe_json(output_text))

    @classmethod
    def _tag_retrieval_io(cls, span, input_text=None, output_documents=None):
        """Tags input text and output documents for retrieval-kind spans.
        Will be mapped to span's `meta.{input,output}.text` fields.
        """
        if input_text is not None:
            span.set_tag_str(INPUT_VALUE, safe_json(input_text))
        if output_documents is None:
            return
        try:
            if not isinstance(output_documents, Documents):
                output_documents = Documents(output_documents)
            if not output_documents.documents:
                return
            span.set_tag_str(OUTPUT_DOCUMENTS, safe_json(output_documents.documents))
        except TypeError:
            log.warning("Failed to parse output documents.", exc_info=True)

    @classmethod
    def _tag_text_io(cls, span, input_value=None, output_value=None):
        """Tags input/output values for non-LLM kind spans.
        Will be mapped to span's `meta.{input,output}.values` fields.
        """
        if input_value is not None:
            span.set_tag_str(INPUT_VALUE, safe_json(input_value))
        if output_value is not None:
            span.set_tag_str(OUTPUT_VALUE, safe_json(output_value))

    @staticmethod
    def _tag_span_tags(span: Span, span_tags: Dict[str, Any]) -> None:
        """Tags a given LLMObs span with a dictionary of key-value tag pairs.
        If tags are already set on the span, the new tags will be merged with the existing tags.
        """
        if not span_tags:
            return
        if not isinstance(span_tags, dict):
            log.warning("span_tags must be a dictionary of string key - primitive value pairs.")
            return
        try:
            current_tags_str = span.get_tag(TAGS)
            if current_tags_str:
                current_tags = json.loads(current_tags_str)
                current_tags.update(span_tags)
                span_tags = current_tags
            span.set_tag_str(TAGS, safe_json(span_tags))
        except Exception:
            log.warning("Failed to parse tags.", exc_info=True)

    @staticmethod
    def _tag_metadata(span: Span, metadata: Dict[str, Any]) -> None:
        """Tags a given LLMObs span with a dictionary of key-value metadata pairs."""
        if not metadata:
            return
        if not isinstance(metadata, dict):
            log.warning("metadata must be a dictionary of string key-value pairs.")
            return
        span.set_tag_str(METADATA, safe_json(metadata))

    @staticmethod
    def _tag_metrics(span: Span, metrics: Dict[str, Any]) -> None:
        """Tags a given LLMObs span with a dictionary of key-value metric pairs."""
        if not metrics:
            return
        if not isinstance(metrics, dict):
            log.warning("metrics must be a dictionary of string key - numeric value pairs.")
            return
        span.set_tag_str(METRICS, safe_json(metrics))

    @classmethod
    def submit_evaluation(
        cls,
        span_context: Dict[str, str],
        label: str,
        metric_type: str,
        value: Union[str, int, float],
        tags: Optional[Dict[str, str]] = None,
        ml_app: Optional[str] = None,
        timestamp_ms: Optional[int] = None,
        metadata: Optional[Dict[str, object]] = None,
    ) -> None:
        """
        Submits a custom evaluation metric for a given span ID and trace ID.

        :param span_context: A dictionary containing the span_id and trace_id of interest.
        :param str label: The name of the evaluation metric.
        :param str metric_type: The type of the evaluation metric. One of "categorical", "score".
        :param value: The value of the evaluation metric.
                      Must be a string (categorical), integer (score), or float (score).
        :param tags: A dictionary of string key-value pairs to tag the evaluation metric with.
        :param str ml_app: The name of the ML application
        :param int timestamp_ms: The timestamp in milliseconds when the evaluation metric result was generated.
        :param dict metadata: A JSON serializable dictionary of key-value metadata pairs relevant to the
                                evaluation metric.
        """
        if cls.enabled is False:
            log.warning(
                "LLMObs.submit_evaluation() called when LLMObs is not enabled. Evaluation metric data will not be sent."
            )
            return
        if not config._dd_api_key:
            log.warning(
                "DD_API_KEY is required for sending evaluation metrics. Evaluation metric data will not be sent. "
                "Ensure this configuration is set before running your application."
            )
            return
        if not isinstance(span_context, dict):
            log.warning(
                "span_context must be a dictionary containing both span_id and trace_id keys. "
                "LLMObs.export_span() can be used to generate this dictionary from a given span."
            )
            return

        ml_app = ml_app if ml_app else config._llmobs_ml_app
        if not ml_app:
            log.warning(
                "ML App name is required for sending evaluation metrics. Evaluation metric data will not be sent. "
                "Ensure this configuration is set before running your application."
            )
            return

        timestamp_ms = timestamp_ms if timestamp_ms else int(time.time() * 1000)

        if not isinstance(timestamp_ms, int) or timestamp_ms < 0:
            log.warning("timestamp_ms must be a non-negative integer. Evaluation metric data will not be sent")
            return

        span_id = span_context.get("span_id")
        trace_id = span_context.get("trace_id")
        if not (span_id and trace_id):
            log.warning("span_id and trace_id must both be specified for the given evaluation metric to be submitted.")
            return
        if not label:
            log.warning("label must be the specified name of the evaluation metric.")
            return

        if not metric_type or metric_type.lower() not in ("categorical", "numerical", "score"):
            log.warning("metric_type must be one of 'categorical' or 'score'.")
            return

        metric_type = metric_type.lower()
        if metric_type == "numerical":
            log.warning(
                "The evaluation metric type 'numerical' is unsupported. Use 'score' instead. "
                "Converting `numerical` metric to `score` type."
            )
            metric_type = "score"

        if metric_type == "categorical" and not isinstance(value, str):
            log.warning("value must be a string for a categorical metric.")
            return
        if metric_type == "score" and not isinstance(value, (int, float)):
            log.warning("value must be an integer or float for a score metric.")
            return
        if tags is not None and not isinstance(tags, dict):
            log.warning("tags must be a dictionary of string key-value pairs.")
            return

        # initialize tags with default values that will be overridden by user-provided tags
        evaluation_tags = {
            "ddtrace.version": ddtrace.__version__,
            "ml_app": ml_app,
        }

        if tags:
            for k, v in tags.items():
                try:
                    evaluation_tags[ensure_text(k)] = ensure_text(v)
                except TypeError:
                    log.warning("Failed to parse tags. Tags for evaluation metrics must be strings.")

        evaluation_metric = {
            "span_id": span_id,
            "trace_id": trace_id,
            "label": str(label),
            "metric_type": metric_type.lower(),
            "timestamp_ms": timestamp_ms,
            "{}_value".format(metric_type): value,
            "ml_app": ml_app,
            "tags": ["{}:{}".format(k, v) for k, v in evaluation_tags.items()],
        }

        if metadata:
            if not isinstance(metadata, dict):
                log.warning("metadata must be json serializable dictionary.")
            else:
                metadata = safe_json(metadata)
                if metadata and isinstance(metadata, str):
                    evaluation_metric["metadata"] = json.loads(metadata)

        cls._instance._llmobs_eval_metric_writer.enqueue(evaluation_metric)

    @classmethod
    def inject_distributed_headers(cls, request_headers: Dict[str, str], span: Optional[Span] = None) -> Dict[str, str]:
        """Injects the span's distributed context into the given request headers."""
        if cls.enabled is False:
            log.warning(
                "LLMObs.inject_distributed_headers() called when LLMObs is not enabled. "
                "Distributed context will not be injected."
            )
            return request_headers
        if not isinstance(request_headers, dict):
            log.warning("request_headers must be a dictionary of string key-value pairs.")
            return request_headers
        if span is None:
            span = cls._instance.tracer.current_span()
        if span is None:
            log.warning("No span provided and no currently active span found.")
            return request_headers
        _inject_llmobs_parent_id(span.context)
        HTTPPropagator.inject(span.context, request_headers)
        return request_headers

    @classmethod
    def activate_distributed_headers(cls, request_headers: Dict[str, str]) -> None:
        """
        Activates distributed tracing headers for the current request.

        :param request_headers: A dictionary containing the headers for the current request.
        """
        if cls.enabled is False:
            log.warning(
                "LLMObs.activate_distributed_headers() called when LLMObs is not enabled. "
                "Distributed context will not be activated."
            )
            return
        context = HTTPPropagator.extract(request_headers)
        if context.trace_id is None or context.span_id is None:
            log.warning("Failed to extract trace ID or span ID from request headers.")
            return
        if PROPAGATED_PARENT_ID_KEY not in context._meta:
            log.warning("Failed to extract LLMObs parent ID from request headers.")
        cls._instance.tracer.context_provider.activate(context)


# initialize the default llmobs instance
LLMObs._instance = LLMObs()
