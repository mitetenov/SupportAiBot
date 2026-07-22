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

            String currentHash = computeHash(resource);
            String storedHash = embeddingService.getFaqHash();
            Integer faqCount = embeddingService.getFaqCount();

            if (currentHash != null && currentHash.equals(storedHash) && faqCount != null && faqCount > 0) {
                log.info("FAQ database is up to date (hash matches, count = {}). Skipping re-indexing.", faqCount);
                embeddingService.markReady();
                return;
            }

            List<Map<String, Object>> entries;
            try (java.io.InputStream is = resource.getInputStream()) {
                entries = objectMapper.readValue(is,
                        new TypeReference<List<Map<String, Object>>>() {
                        }
                );
            }

            log.info("Indexing {} FAQ entries (stored hash = {}, current hash = {})", entries.size(), storedHash, currentHash);

            embeddingService.clearFaq();

            for (Map<String, Object> entry : entries) {
                String question = (String) entry.get("question");
                String answer = (String) entry.get("answer");
                if (question != null && answer != null) {
                    String images = extractImages(entry.get("images"));
                    String keywords = extractKeywords(entry.get("keywords"));
                    embeddingService.indexFaq(question, answer, images, keywords);
                }
            }

            if (currentHash != null) {
                embeddingService.updateFaqHash(currentHash);
            }

            log.info("FAQ indexing complete: {} entries", entries.size());
            embeddingService.markReady();
        } catch (Exception e) {
            log.error("Failed to load FAQ file — FAQ search will be unavailable", e);
        }
    }

    private String computeHash(ClassPathResource resource) {
        try (java.io.InputStream is = resource.getInputStream()) {
            java.security.MessageDigest digest = java.security.MessageDigest.getInstance("SHA-256");
            byte[] block = new byte[4096];
            int length;
            while ((length = is.read(block)) > 0) {
                digest.update(block, 0, length);
            }
            byte[] hash = digest.digest();
            StringBuilder hexString = new StringBuilder();
            for (byte b : hash) {
                String hex = Integer.toHexString(0xff & b);
                if (hex.length() == 1) hexString.append('0');
                hexString.append(hex);
            }
            return hexString.toString();
        } catch (Exception e) {
            log.error("Failed to compute FAQ file hash", e);
            return null;
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

    private static String extractKeywords(Object keywordsValue) {
        if (keywordsValue instanceof List<?> list && !list.isEmpty()) {
            return list.stream()
                    .map(Object::toString)
                    .collect(java.util.stream.Collectors.joining(", "));
        } else if (keywordsValue instanceof String str && !str.isBlank()) {
            return str.trim();
        }
        return null;
    }
}
