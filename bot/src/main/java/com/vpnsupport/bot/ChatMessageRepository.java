package com.vpnsupport.bot;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.List;

@Repository
public interface ChatMessageRepository extends JpaRepository<ChatMessage, Long> {

    /**
     * Returns the most recent messages, newest first. Callers must reverse the
     * result to get chronological order — ordering ascending here would take the
     * 20 <em>oldest</em> messages in the TTL window instead of the live
     * conversation.
     */
    List<ChatMessage> findTop20ByTelegramIdOrderByCreatedAtDesc(Long telegramId);

    @Modifying
    @Transactional
    @Query("DELETE FROM ChatMessage WHERE createdAt < :cutoff")
    void deleteByCreatedAtBefore(@Param("cutoff") Instant cutoff);

    @Modifying
    @Transactional
    @Query("DELETE FROM ChatMessage WHERE telegramId = :telegramId")
    void deleteByTelegramId(@Param("telegramId") Long telegramId);
}
