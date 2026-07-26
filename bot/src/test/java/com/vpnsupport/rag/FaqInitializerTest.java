package com.vpnsupport.rag;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.io.TempDir;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.core.io.ClassPathResource;

import java.io.IOException;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

@ExtendWith(MockitoExtension.class)
class FaqInitializerTest {

    @Mock
    private FaqEmbeddingService embeddingService;

    private ObjectMapper objectMapper;
    private FaqInitializer initializer;

    @BeforeEach
    void setUp() {
        objectMapper = new ObjectMapper();
        initializer = new FaqInitializer(embeddingService, objectMapper);
    }

    @Test
    void shouldComputeConsistentHashForSameContent(@TempDir Path tempDir) throws Exception {
        Path file = tempDir.resolve("test-faq.json");
        Files.writeString(file, "[{\"question\":\"Q1\",\"answer\":\"A1\"}]");
        ClassPathResource resource = new ClassPathResource("faq/faq.json") {
            private final Path actualFile = file;

            @Override
            public java.io.InputStream getInputStream() throws IOException {
                return Files.newInputStream(actualFile);
            }

            @Override
            public boolean exists() {
                return true;
            }
        };

        String hash1 = invokeComputeHash(resource);
        String hash2 = invokeComputeHash(resource);

        assertNotNull(hash1);
        assertNotNull(hash2);
        assertEquals(hash1, hash2);
    }

    @Test
    void shouldComputeDifferentHashForDifferentContent(@TempDir Path tempDir) throws Exception {
        Path file1 = tempDir.resolve("test-faq1.json");
        Path file2 = tempDir.resolve("test-faq2.json");
        Files.writeString(file1, "[{\"question\":\"Q1\",\"answer\":\"A1\"}]");
        Files.writeString(file2, "[{\"question\":\"Q2\",\"answer\":\"A2\"}]");

        String hash1 = invokeComputeHash(createResource(file1));
        String hash2 = invokeComputeHash(createResource(file2));

        assertNotNull(hash1);
        assertNotNull(hash2);
        assertNotEquals(hash1, hash2);
    }

    @Test
    void shouldComputeHashReturnNonNullForValidFile(@TempDir Path tempDir) throws Exception {
        Path file = tempDir.resolve("test-faq.json");
        Files.writeString(file, "[]");
        ClassPathResource resource = createResource(file);

        String hash = invokeComputeHash(resource);

        assertNotNull(hash);
        assertEquals(64, hash.length());
    }

    private String invokeComputeHash(ClassPathResource resource) throws Exception {
        Method method = FaqInitializer.class.getDeclaredMethod("computeHash", ClassPathResource.class);
        method.setAccessible(true);
        return (String) method.invoke(initializer, resource);
    }

    private ClassPathResource createResource(Path file) {
        return new ClassPathResource("faq/faq.json") {
            private final Path actualFile = file;

            @Override
            public java.io.InputStream getInputStream() throws IOException {
                return Files.newInputStream(actualFile);
            }

            @Override
            public boolean exists() {
                return true;
            }
        };
    }
}
