import os
from litellm.integrations.custom_logger import CustomLogger
from langfuse import Langfuse
LOG_CONTENT = os.environ.get("LANGFUSE_LOG_CONTENT", "false").lower() == "true"

class MetadataOnlyLangfuseCallback(CustomLogger):
    def __init__(self):
        self.langfuse = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            slo = kwargs.get("standard_logging_object") or {}

            # response_obj.usage is populated for non-streaming; slo has top-level token fields for streaming
            usage = getattr(response_obj, "usage", None)
            if usage and getattr(usage, "prompt_tokens", 0):
                prompt_tokens = getattr(usage, "prompt_tokens", 0)
                completion_tokens = getattr(usage, "completion_tokens", 0)
                total_tokens = getattr(usage, "total_tokens", 0)
            else:
                prompt_tokens = slo.get("prompt_tokens", 0)
                completion_tokens = slo.get("completion_tokens", 0)
                total_tokens = slo.get("total_tokens", 0)

            call_id = slo.get("id") or kwargs.get("litellm_call_uid")
            trace_input = None
            trace_output = None
            if LOG_CONTENT:
                trace_input = kwargs.get("messages")
                resp = kwargs.get("complete_streaming_response") or response_obj
                try:
                    trace_output = resp.choices[0].message.content
                except Exception:
                    trace_output = slo.get("response")

            trace = self.langfuse.trace(
                name="litellm-acompletion",
                input=trace_input,
                output=trace_output,
                metadata={
                    "provider": kwargs.get("custom_llm_provider"),
                    "stream": kwargs.get("stream", False),
                },
            )
            trace.generation(
                name="completion",
                model=kwargs.get("model", "unknown"),
                input=trace_input,
                output=trace_output,
                start_time=start_time,
                end_time=end_time,
                usage={
                    "input": prompt_tokens,
                    "output": completion_tokens,
                    "total": total_tokens,
                    "unit": "TOKENS",
                },
                metadata={
                    "cost_usd": kwargs.get("response_cost"),
                    "call_id": call_id,
                },
            )
        except Exception as e:
            print(f"[MetadataOnlyLangfuseCallback] logging error: {e}")


proxy_handler_instance = MetadataOnlyLangfuseCallback()
