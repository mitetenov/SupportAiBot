package com.vpnsupport.llm;

/**
 * A failure talking to an MCP server: transport error, protocol error, or an
 * error object returned by the server itself.
 *
 * <p>Distinct from a bare {@link RuntimeException} so callers can tell an MCP
 * problem apart from a bug in the surrounding code — {@link HttpMcpClient}
 * catches it to report a tool failure to the model rather than aborting the
 * whole conversation.
 */
public class McpException extends RuntimeException {

    public McpException(String message) {
        super(message);
    }

    public McpException(String message, Throwable cause) {
        super(message, cause);
    }
}
