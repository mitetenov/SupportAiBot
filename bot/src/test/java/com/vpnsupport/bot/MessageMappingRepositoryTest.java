package com.vpnsupport.bot;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest;

import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;

@DataJpaTest
class MessageMappingRepositoryTest {

    @Autowired
    private MessageMappingRepository repository;

    @Test
    void findByUserChatIdAndUserMessageId_returnsMappingWhenExists() {
        // Arrange
        MessageMapping saved = repository.save(
                new MessageMapping(100, 200, 12345L, 42)
        );

        // Act
        Optional<MessageMapping> result = repository.findByUserChatIdAndUserMessageId("12345", 42);

        // Assert
        assertThat(result).isPresent();
        assertThat(result.get().getId()).isEqualTo(saved.getId());
        assertThat(result.get().getUserChatId()).isEqualTo(12345L);
        assertThat(result.get().getUserMessageId()).isEqualTo(42);
        assertThat(result.get().getTopicMessageId()).isEqualTo(100);
        assertThat(result.get().getTopicId()).isEqualTo(200);
    }

    @Test
    void findByUserChatIdAndUserMessageId_returnsEmptyWhenNotFound() {
        // Act
        Optional<MessageMapping> result = repository.findByUserChatIdAndUserMessageId("99999", 999);

        // Assert
        assertThat(result).isEmpty();
    }

    @Test
    void findByUserChatIdAndUserMessageId_returnsEmptyWhenChatIdMismatches() {
        // Arrange
        repository.save(new MessageMapping(100, 200, 12345L, 42));

        // Act — same message ID but wrong chat ID
        Optional<MessageMapping> result = repository.findByUserChatIdAndUserMessageId("99999", 42);

        // Assert
        assertThat(result).isEmpty();
    }

    @Test
    void findByUserChatIdAndUserMessageId_returnsEmptyWhenMessageIdMismatches() {
        // Arrange
        repository.save(new MessageMapping(100, 200, 12345L, 42));

        // Act — same chat ID but wrong message ID
        Optional<MessageMapping> result = repository.findByUserChatIdAndUserMessageId("12345", 999);

        // Assert
        assertThat(result).isEmpty();
    }
}
