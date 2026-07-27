package com.vpnsupport.rag;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.vpnsupport.config.OpenAiProperties;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import okhttp3.mockwebserver.RecordedRequest;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class OpenAiEmbeddingProviderTest {

    private MockWebServer server;
    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() throws Exception {
        server = new MockWebServer();
        server.start();
        objectMapper = new ObjectMapper();
    }

    @AfterEach
    void tearDown() throws Exception {
        server.shutdown();
    }

    private OpenAiEmbeddingProvider createProvider(String model) {
        OpenAiProperties properties = new OpenAiProperties();
        properties.setBaseUrl("http://localhost:" + server.getPort());
        properties.setApiKey("sk-test-key");
        properties.setEmbeddingModel(model);
        return new OpenAiEmbeddingProvider(objectMapper, properties);
    }

    @Test
    void shouldReturnDimension1536() {
        OpenAiEmbeddingProvider provider = createProvider("text-embedding-3-small");
        assertEquals(1536, provider.getDimension());
    }

    @Test
    void shouldUseDefaultModelWhenConfigNull() {
        OpenAiProperties properties = new OpenAiProperties();
        properties.setBaseUrl("http://localhost:" + server.getPort());
        properties.setApiKey("sk-test-key");
        properties.setEmbeddingModel(null);
        OpenAiEmbeddingProvider provider = new OpenAiEmbeddingProvider(objectMapper, properties);

        server.enqueue(new MockResponse()
                .setBody("""
                        {"data":[{"embedding":[1.0]}]}
                        """)
                .addHeader("Content-Type", "application/json"));

        provider.embed("test");

        assertDoesNotThrow(() -> {
            RecordedRequest request = server.takeRequest();
            String body = request.getBody().readUtf8();
            assertTrue(body.contains("\"text-embedding-3-small\""));
        });
    }

    @Test
    void shouldUseDefaultModelWhenConfigBlank() {
        OpenAiProperties properties = new OpenAiProperties();
        properties.setBaseUrl("http://localhost:" + server.getPort());
        properties.setApiKey("sk-test-key");
        properties.setEmbeddingModel("  ");
        OpenAiEmbeddingProvider provider = new OpenAiEmbeddingProvider(objectMapper, properties);

        server.enqueue(new MockResponse()
                .setBody("""
                        {"data":[{"embedding":[1.0]}]}
                        """)
                .addHeader("Content-Type", "application/json"));

        provider.embed("test");

        assertDoesNotThrow(() -> {
            RecordedRequest request = server.takeRequest();
            String body = request.getBody().readUtf8();
            assertTrue(body.contains("\"text-embedding-3-small\""), "Should default to text-embedding-3-small when config is blank");
        });
    }

    @Test
    void shouldUseConfiguredModel() throws Exception {
        OpenAiEmbeddingProvider provider = createProvider("text-embedding-3-large");

        server.enqueue(new MockResponse()
                .setBody("""
                        {"data":[{"embedding":[1.0]}]}
                        """)
                .addHeader("Content-Type", "application/json"));

        provider.embed("test");

        RecordedRequest request = server.takeRequest();
        String body = request.getBody().readUtf8();
        assertTrue(body.contains("\"text-embedding-3-large\""));
    }

    @Test
    void shouldReturnEmptyArrayOnHttpError() {
        OpenAiEmbeddingProvider provider = createProvider("text-embedding-3-small");
        server.enqueue(new MockResponse().setResponseCode(500));

        float[] result = provider.embed("test");

        assertEquals(0, result.length);
    }

    @Test
    void shouldReturnEmptyArrayWhenNoDataInResponse() {
        OpenAiEmbeddingProvider provider = createProvider("text-embedding-3-small");
        server.enqueue(new MockResponse()
                .setBody("""
                        {"model":"text-embedding-3-small"}
                        """)
                .addHeader("Content-Type", "application/json"));

        float[] result = provider.embed("test");

        assertEquals(0, result.length);
    }

    @Test
    void shouldReturnEmptyArrayWhenEmptyDataArray() {
        OpenAiEmbeddingProvider provider = createProvider("text-embedding-3-small");
        server.enqueue(new MockResponse()
                .setBody("""
                        {"data":[]}
                        """)
                .addHeader("Content-Type", "application/json"));

        float[] result = provider.embed("test");

        assertEquals(0, result.length);
    }

    @Test
    void shouldReturnEmptyArrayWhenNoEmbeddingInDataItem() {
        OpenAiEmbeddingProvider provider = createProvider("text-embedding-3-small");
        server.enqueue(new MockResponse()
                .setBody("""
                        {"data":[{"index":0}]}
                        """)
                .addHeader("Content-Type", "application/json"));

        float[] result = provider.embed("test");

        assertEquals(0, result.length);
    }

    @Test
    void shouldReturnEmbeddingOnSuccess() throws Exception {
        OpenAiEmbeddingProvider provider = createProvider("text-embedding-3-small");

        server.enqueue(new MockResponse()
                .setBody("""
                        {"data":[{"embedding":[0.5,0.6,0.7]}]}
                        """)
                .addHeader("Content-Type", "application/json"));

        float[] result = provider.embed("hello");

        assertNotNull(result);
        assertEquals(3, result.length);
        assertEquals(0.5f, result[0], 0.0001f);
        assertEquals(0.6f, result[1], 0.0001f);
        assertEquals(0.7f, result[2], 0.0001f);

        RecordedRequest request = server.takeRequest();
        assertEquals("POST", request.getMethod());
        assertEquals("/embeddings", request.getPath());
        assertEquals("Bearer sk-test-key", request.getHeader("Authorization"));

        String body = request.getBody().readUtf8();
        assertTrue(body.contains("\"model\":\"text-embedding-3-small\""));
        assertTrue(body.contains("\"input\":\"hello\""));
        assertTrue(body.contains("\"dimensions\":1536"));
    }

    @Test
    void shouldUseBearerAuthHeader() throws Exception {
        OpenAiEmbeddingProvider provider = createProvider("text-embedding-3-small");

        server.enqueue(new MockResponse()
                .setBody("""
                        {"data":[{"embedding":[0.1]}]}
                        """)
                .addHeader("Content-Type", "application/json"));

        provider.embed("auth test");

        RecordedRequest request = server.takeRequest();
        assertEquals("Bearer sk-test-key", request.getHeader("Authorization"));
    }

    @Test
    void shouldReturnEmptyArrayOnMalformedJson() {
        OpenAiEmbeddingProvider provider = createProvider("text-embedding-3-small");
        server.enqueue(new MockResponse()
                .setBody("not json")
                .addHeader("Content-Type", "text/plain"));

        float[] result = provider.embed("test");

        assertEquals(0, result.length);
    }
}
