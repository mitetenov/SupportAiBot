package com.vpnsupport.rag;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.core.io.ClassPathResource;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Map;

@Component
public class FaqInitializer implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(FaqInitializer.class);
    private static final String FAQ_CLASSPATH = "faq/faq.json";

    private final FaqEmbeddingService embeddingService;
    private final ObjectMapper objectMapper;

    public FaqInitializer(FaqEmbeddingService embeddingService, ObjectMapper objectMapper) {
        this.embeddingService = embeddingService;
        this.objectMapper = objectMapper;
    }

    @Override
    public void run(ApplicationArguments args) {
        ClassPathResource resource = new ClassPathResource(FAQ_CLASSPATH);
        if (!resource.exists()) {
            log.warn("FAQ file not found at classpath:{}", FAQ_CLASSPATH);
            embeddingService.markReady();
            return;
        }

        try {
            embeddingService.initSchema();

            List<Map<String, Object>> entries;
            try (java.io.InputStream is = resource.getInputStream()) {
                entries = objectMapper.readValue(is,
                        new TypeReference<List<Map<String, Object>>>() {
                        }
                );
            }

            log.info("Indexing {} FAQ entries", entries.size());

            embeddingService.clearFaq();

            for (Map<String, Object> entry : entries) {
                String question = (String) entry.get("question");
                String answer = (String) entry.get("answer");
                if (question != null && answer != null) {
                    String images = extractImages(entry.get("images"));
                    embeddingService.indexFaq(question, answer, images);
                }
            }

            log.info("FAQ indexing complete: {} entries", entries.size());
            embeddingService.markReady();
        } catch (Exception e) {
            log.error("Failed to load FAQ file — FAQ search will be unavailable", e);
        }
    }

    private static String extractImages(Object imagesValue) {
        if (imagesValue instanceof List<?> list && !list.isEmpty()) {
            return list.stream()
                    .map(Object::toString)
                    .collect(java.util.stream.Collectors.joining(","));
        }
        return null;
    }
}
