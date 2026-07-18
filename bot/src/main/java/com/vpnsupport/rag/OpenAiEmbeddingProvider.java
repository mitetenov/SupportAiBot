package com.vpnsupport.rag;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.vpnsupport.config.OpenAiProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.netty.http.client.HttpClient;

import java.time.Duration;

@Component
@ConditionalOnProperty(name = "embedding.provider", havingValue = "openai")
public class OpenAiEmbeddingProvider implements EmbeddingProvider {

    private static final Logger log = LoggerFactory.getLogger(OpenAiEmbeddingProvider.class);
    private static final int DIMENSION = 1536;

    private final ObjectMapper objectMapper;
    private final WebClient webClient;
    private final String model;

    public OpenAiEmbeddingProvider(ObjectMapper objectMapper, OpenAiProperties openAiProperties) {
        this.objectMapper = objectMapper;
        this.model = openAiProperties.getEmbeddingModel() != null && !openAiProperties.getEmbeddingModel().isBlank()
                ? openAiProperties.getEmbeddingModel()
                : "text-embedding-3-small";
        this.webClient = WebClient.builder()
                .baseUrl(openAiProperties.getBaseUrl())
                .defaultHeader("Authorization", "Bearer " + openAiProperties.getApiKey())
                .defaultHeader("Content-Type", "application/json")
                .clientConnector(new org.springframework.http.client.reactive.ReactorClientHttpConnector(
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
            requestBody.put("model", model);
            requestBody.put("input", text);
            requestBody.put("dimensions", DIMENSION);

            String response = webClient.post()
                    .uri("/embeddings")
                    .bodyValue(requestBody)
                    .retrieve()
                    .bodyToMono(String.class)
                    .block();

            JsonNode jsonResponse = objectMapper.readTree(response);
            JsonNode data = jsonResponse.get("data");
            if (data == null || !data.isArray() || data.isEmpty()) {
                log.error("No embedding data in OpenAI response: {}", response);
                return null;
            }
            JsonNode embeddingNode = data.get(0).get("embedding");
            if (embeddingNode == null || !embeddingNode.isArray()) {
                log.error("Unexpected OpenAI embedding response: {}", response);
                return null;
            }

            float[] result = new float[embeddingNode.size()];
            for (int i = 0; i < embeddingNode.size(); i++) {
                result[i] = embeddingNode.get(i).floatValue();
            }
            return result;

        } catch (Exception e) {
            log.error("OpenAI embedding request failed", e);
            return null;
        }
    }
}
