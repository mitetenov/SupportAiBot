package com.vpnsupport.llm;

import java.util.List;
import java.util.Map;

public interface McpClientInterface {

    List<McpTool> listTools();

    String callTool(String toolName, Map<String, Object> arguments);
}
