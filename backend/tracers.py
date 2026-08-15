from agents import TracingProcessor, Trace, Span
from .database import write_log
from .mcp_servers import INTERNAL_MCP_SERVERS
import secrets
import string

ALPHANUM = string.ascii_lowercase + string.digits

# The log type carrying external MCP tool calls. It was the SDK's own type for the
# list_tools handshake, which we now drop (see on_span_start), so nothing else uses it.
MCP_LOG_TYPE = "mcp_tools"

# Which server exposes which tool, learned from the list_tools span the SDK runs against
# every server before the agent may call anything on it. That span is the only place the
# mapping appears early enough to classify a call as it *starts* - a tool call's own
# `mcp_data` is not filled in until the call ends. Module level, so it survives across
# runs and across the four traders; MCP tool names are global to a server, not per trader.
_tool_servers: dict[str, str] = {}


def pretty_tool_name(name: str) -> str:
    """`tavily_search` -> `Tavily Search`, for a log line meant to be read at a glance."""
    return name.replace("_", " ").title()


def make_trace_id(tag: str) -> str:
    """
    Return a string of the form 'trace_<tag><random>',
    where the total length after 'trace_' is 32 chars.
    """
    tag += "0"
    pad_len = 32 - len(tag)
    random_suffix = ''.join(secrets.choice(ALPHANUM) for _ in range(pad_len))
    return f"trace_{tag}{random_suffix}"


class LogTracer(TracingProcessor):

    def get_name(self, trace_or_span: Trace | Span) -> str | None:
        trace_id = trace_or_span.trace_id
        name = trace_id.split("_")[1]
        if '0' in name:
            return name.split("0")[0]
        else:
            return None

    def on_trace_start(self, trace) -> None:
        name = self.get_name(trace)
        if name:
            write_log(name, "trace", f"Started: {trace.name}")

    def on_trace_end(self, trace) -> None:
        name = self.get_name(trace)
        if name:
            write_log(name, "trace", f"Ended: {trace.name}")

    def remember_tools(self, span_data) -> None:
        """Record which tools the just-listed server owns, for external_tool_server."""
        server = getattr(span_data, "server", None)
        for tool in getattr(span_data, "result", None) or []:
            _tool_servers[tool] = server

    def external_tool_server(self, span_data) -> str | None:
        """The third-party MCP server behind this span, or None if it isn't one.

        None covers three cases that all belong in the plain `function` log: our own
        servers, the Researcher sub-agent (a local `as_tool`, never in _tool_servers),
        and any tool whose server we never saw listed.
        """
        server = _tool_servers.get(getattr(span_data, "name", None) or "")
        return None if server is None or server in INTERNAL_MCP_SERVERS else server

    def write_span(self, span, verb: str) -> None:
        name = self.get_name(span)
        if not name:
            return
        span_data = span.span_data
        type = span_data.type if span_data else "span"

        # The list_tools handshake. It fires once per server per run and reports only
        # which server was probed, so it drowned the panel without saying anything about
        # what the agent did. Harvest the mapping it carries and drop the line.
        if type == "mcp_tools":
            self.remember_tools(span_data)
            return

        server = self.external_tool_server(span_data) if type == "function" else None
        if server:
            # e.g. "Tavily Search Started", under an MCP_TOOLS label.
            message = f"{pretty_tool_name(span_data.name)} {verb}"
            if span.error:
                message += f" {span.error}"
            write_log(name, MCP_LOG_TYPE, message)
            return

        message = verb
        if span_data:
            if span_data.type:
                message += f" {span_data.type}"
            if getattr(span_data, "name", None):
                message += f" {span_data.name}"
            if getattr(span_data, "server", None):
                message += f" {span_data.server}"
        if span.error:
            message += f" {span.error}"
        write_log(name, type, message)

    def on_span_start(self, span) -> None:
        self.write_span(span, "Started")

    def on_span_end(self, span) -> None:
        self.write_span(span, "Ended")

    def force_flush(self) -> None:
        pass

    def shutdown(self) -> None:
        pass
