package com.vpnsupport.rag;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.config.GeminiProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.client.reactive.ReactorClientHttpConnector;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.netty.http.client.HttpClient;

import java.time.Duration;

@Component
@ConditionalOnProperty(name = "embedding.provider", havingValue = "gemini", matchIfMissing = true)
public class GeminiEmbeddingProvider implements EmbeddingProvider {

    private static final Logger log = LoggerFactory.getLogger(GeminiEmbeddingProvider.class);
    private static final int DIMENSION = 2000;
    private static final String MODEL = "gemini-embedding-001";

    private final ObjectMapper objectMapper;
    private final WebClient webClient;

    public GeminiEmbeddingProvider(ObjectMapper objectMapper, GeminiProperties geminiProperties) {
        this.objectMapper = objectMapper;
        this.webClient = WebClient.builder()
                .baseUrl(geminiProperties.getBaseUrl())
                .defaultHeader("x-goog-api-key", geminiProperties.getApiKey())
                .clientConnector(new ReactorClientHttpConnector(
                        HttpClient.create().responseTimeout(Duration.ofSeconds(60))))
                .build();
    }

    @Override
    public int getDimension() {
        return DIMENSION;
    }

    @Override
    public float[] embed(String text) {
        try {
            ObjectNode requestBody = objectMapper.createObjectNode();
            ObjectNode content = requestBody.putObject("content");
            ArrayNode parts = content.putArray("parts");
            parts.addObject().put("text", text);
            requestBody.put("outputDimensionality", DIMENSION);

            String response = webClient.post()
                    .uri("/models/{model}:embedContent", MODEL)
                    .header("Content-Type", "application/json")
                    .bodyValue(requestBody)
                    .retrieve()
                    .bodyToMono(String.class)
                    .block();

            JsonNode jsonResponse = objectMapper.readTree(response);
            JsonNode embeddingNode = jsonResponse.get("embedding");
            if (embeddingNode == null) {
                log.error("No embedding in Gemini response: {}", response);
                return null;
            }
            JsonNode values = embeddingNode.get("values");
            if (values == null || !values.isArray()) {
                log.error("Unexpected Gemini embedding response: {}", response);
                return null;
            }

            float[] result = new float[values.size()];
            for (int i = 0; i < values.size(); i++) {
                result[i] = values.get(i).floatValue();
            }
            return result;

        } catch (Exception e) {
            log.error("Gemini embedding request failed", e);
            return null;
        }
    }
}
