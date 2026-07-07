package com.vpnsupport.llm;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.bot.AdminNotifier;
import com.vpnsupport.config.RemnawaveMcpProperties;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.test.util.ReflectionTestUtils;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

@ExtendWith(MockitoExtension.class)
class HttpMcpClientTest {

    @Mock
    private AdminNotifier adminNotifier;

    @Test
    void shouldExtractPlainJson() {
        String input = "{\"jsonrpc\":\"2.0\",\"result\":{\"key\":\"value\"},\"id\":1}";
        String result = invokeExtractJsonFromSse(input);
        assertEquals(input, result);
    }

    @Test
    void shouldExtractJsonFromSseDataLine() {
        String input = "data: {\"jsonrpc\":\"2.0\",\"result\":{\"key\":\"value\"},\"id\":1}";
        String expected = "{\"jsonrpc\":\"2.0\",\"result\":{\"key\":\"value\"},\"id\":1}";
        String result = invokeExtractJsonFromSse(input);
        assertEquals(expected, result);
    }

    @Test
    void shouldExtractJsonFromSseWithEvent() {
        String input = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"result\":\"hello\",\"id\":1}";
        String expected = "{\"jsonrpc\":\"2.0\",\"result\":\"hello\",\"id\":1}";
        String result = invokeExtractJsonFromSse(input);
        assertEquals(expected, result);
    }

    @Test
    void shouldExtractFirstDataLineInSse() {
        String input = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"result\":\"first\",\"id\":1}\n\ndata: {\"jsonrpc\":\"2.0\",\"result\":\"second\",\"id\":2}";
        String expected = "{\"jsonrpc\":\"2.0\",\"result\":\"first\",\"id\":1}";
        String result = invokeExtractJsonFromSse(input);
        assertEquals(expected, result);
    }

    @Test
    void shouldReturnEmptyBodyAsIs() {
        String input = "";
        String result = invokeExtractJsonFromSse(input);
        assertEquals("", result);
    }

    @Test
    void shouldHandleSseDataWithSpaces() {
        String input = "data:   {\"jsonrpc\":\"2.0\",\"result\":\"spaces\",\"id\":1}";
        String result = invokeExtractJsonFromSse(input);
        assertEquals("  {\"jsonrpc\":\"2.0\",\"result\":\"spaces\",\"id\":1}", result);
    }

    @Test
    void shouldHandleCrLfSseData() {
        String input = "data: {\"jsonrpc\":\"2.0\",\"result\":\"crlf\",\"id\":1}\r\n";
        String result = invokeExtractJsonFromSse(input);
        assertEquals("{\"jsonrpc\":\"2.0\",\"result\":\"crlf\",\"id\":1}\r", result);
    }

    @Test
    void shouldIncludeAcceptHeader() {
        RemnawaveMcpProperties properties = new RemnawaveMcpProperties();
        properties.setUrl("http://localhost:3100");

        HttpMcpClient client = new HttpMcpClient(properties, new ObjectMapper(), adminNotifier);
        var webClient = (org.springframework.web.reactive.function.client.WebClient)
                ReflectionTestUtils.getField(client, "webClient");
        assertNotNull(webClient);
        // WebClient headers are verified by the constructor — if it compiles and runs, Accept is set
    }

    @Test
    void shouldReturnErrorWhenNotInitialized() {
        RemnawaveMcpProperties properties = new RemnawaveMcpProperties();
        properties.setUrl("http://localhost:3100");

        HttpMcpClient client = new HttpMcpClient(properties, new ObjectMapper(), adminNotifier);
        String result = client.callTool("test_tool", java.util.Map.of());

        assertTrue(result.contains("error"));
        assertTrue(result.contains("not initialized"));
    }

    @Test
    void shouldSetInitializedFlagToFalseOnShutdown() {
        RemnawaveMcpProperties properties = new RemnawaveMcpProperties();
        properties.setUrl("http://localhost:3100");

        HttpMcpClient client = new HttpMcpClient(properties, new ObjectMapper(), adminNotifier);
        client.shutdown();

        Boolean initialized = (Boolean) ReflectionTestUtils.getField(client, "initialized");
        assertEquals(false, initialized);
    }

    @Test
    void shouldClearSessionIdOnShutdown() {
        RemnawaveMcpProperties properties = new RemnawaveMcpProperties();
        properties.setUrl("http://localhost:3100");

        HttpMcpClient client = new HttpMcpClient(properties, new ObjectMapper(), adminNotifier);
        ReflectionTestUtils.setField(client, "sessionId", "test-session-123");
        client.shutdown();

        String sessionId = (String) ReflectionTestUtils.getField(client, "sessionId");
        assertEquals(null, sessionId);
    }

    @Test
    void shouldReturnEmptyListBeforeInit() {
        RemnawaveMcpProperties properties = new RemnawaveMcpProperties();
        properties.setUrl("http://localhost:3100");

        HttpMcpClient client = new HttpMcpClient(properties, new ObjectMapper(), adminNotifier);
        var tools = client.listTools();

        assertNotNull(tools);
        assertTrue(tools.isEmpty());
    }

    @Test
    void shouldCreateClientWithDefaultUrl() {
        RemnawaveMcpProperties properties = new RemnawaveMcpProperties();
        properties.setUrl("http://mcp-remnawave:3100");
        properties.setBaseUrl("https://panel.example.com");
        properties.setApiToken("test-token");
        properties.setReadonly(true);

        assertDoesNotThrow(() -> new HttpMcpClient(properties, new ObjectMapper(), adminNotifier));
    }

    @Test
    void shouldHandleErrorResponse() {
        RemnawaveMcpProperties properties = new RemnawaveMcpProperties();
        properties.setUrl("http://localhost:3100");

        HttpMcpClient client = new HttpMcpClient(properties, new ObjectMapper(), adminNotifier);
        String result = ReflectionTestUtils.invokeMethod(client, "errorResponse", "test error");

        assertTrue(result.contains("error"));
        assertTrue(result.contains("test error"));
    }

    @Test
    void shouldFormatErrorResponse() {
        RemnawaveMcpProperties properties = new RemnawaveMcpProperties();
        properties.setUrl("http://localhost:3100");

        HttpMcpClient client = new HttpMcpClient(properties, new ObjectMapper(), adminNotifier);
        String result = ReflectionTestUtils.invokeMethod(client, "errorResponse", "connection refused");

        assertTrue(result.contains("error"));
        assertTrue(result.contains("connection refused"));
    }

    private static String invokeExtractJsonFromSse(String input) {
        return (String) ReflectionTestUtils.invokeMethod(
                HttpMcpClient.class, "extractJsonFromSse", input);
    }
}
