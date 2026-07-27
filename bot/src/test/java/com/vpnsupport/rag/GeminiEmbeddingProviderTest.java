package com.vpnsupport.rag;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.config.GeminiProperties;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import okhttp3.mockwebserver.RecordedRequest;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class GeminiEmbeddingProviderTest {

    private MockWebServer server;
    private GeminiEmbeddingProvider provider;
    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() throws Exception {
        server = new MockWebServer();
        server.start();

        GeminiProperties properties = new GeminiProperties();
        properties.setBaseUrl("http://localhost:" + server.getPort());
        properties.setApiKey("test-api-key");

        objectMapper = new ObjectMapper();
        provider = new GeminiEmbeddingProvider(objectMapper, properties);
    }

    @AfterEach
    void tearDown() throws Exception {
        server.shutdown();
    }

    @Test
    void shouldReturnDimension2000() {
        assertEquals(2000, provider.getDimension());
    }

    @Test
    void shouldReturnEmbeddingOnSuccess() throws Exception {
        server.enqueue(new MockResponse()
                .setBody("""
                        {"embedding":{"values":[0.1,0.2,0.3]}}
                        """)
                .addHeader("Content-Type", "application/json"));

        float[] result = provider.embed("test text");

        assertNotNull(result);
        assertEquals(3, result.length);
        assertEquals(0.1f, result[0], 0.0001f);
        assertEquals(0.2f, result[1], 0.0001f);
        assertEquals(0.3f, result[2], 0.0001f);

        RecordedRequest request = server.takeRequest();
        assertEquals("POST", request.getMethod());
        assertTrue(request.getPath().contains(":embedContent"));
        assertTrue(request.getPath().contains("gemini-embedding-001"));
        assertEquals("test-api-key", request.getHeader("x-goog-api-key"));

        String body = request.getBody().readUtf8();
        assertTrue(body.contains("\"text\":\"test text\""));
        assertTrue(body.contains("\"outputDimensionality\":2000"));
    }

    @Test
    void shouldReturnEmptyArrayOnHttpError() {
        server.enqueue(new MockResponse().setResponseCode(500));

        float[] result = provider.embed("test");

        assertEquals(0, result.length);
    }

    @Test
    void shouldReturnEmptyArrayWhenNoEmbeddingKey() {
        server.enqueue(new MockResponse()
                .setBody("""
                        {"somethingElse":true}
                        """)
                .addHeader("Content-Type", "application/json"));

        float[] result = provider.embed("test");

        assertEquals(0, result.length);
    }

    @Test
    void shouldReturnEmptyArrayWhenValuesNotArray() {
        server.enqueue(new MockResponse()
                .setBody("""
                        {"embedding":{"values":"not-an-array"}}
                        """)
                .addHeader("Content-Type", "application/json"));

        float[] result = provider.embed("test");

        assertEquals(0, result.length);
    }

    @Test
    void shouldReturnEmptyArrayOnMalformedJson() {
        server.enqueue(new MockResponse()
                .setBody("not json at all")
                .addHeader("Content-Type", "text/plain"));

        float[] result = provider.embed("test");

        assertEquals(0, result.length);
    }

    @Test
    void shouldSendContentPartsStructure() throws Exception {
        server.enqueue(new MockResponse()
                .setBody("""
                        {"embedding":{"values":[1.0]}}
                        """)
                .addHeader("Content-Type", "application/json"));

        provider.embed("hello world");

        RecordedRequest request = server.takeRequest();
        String body = request.getBody().readUtf8();

        assertTrue(body.contains("\"content\""));
        assertTrue(body.contains("\"parts\""));
    }
}
