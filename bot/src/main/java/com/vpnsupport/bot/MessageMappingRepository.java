package com.vpnsupport.bot;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.util.Optional;

@Repository
public interface MessageMappingRepository extends JpaRepository<MessageMapping, Long> {

    /**
     * Look up the mapping for a message in a given topic thread.
     *
     * @param topicMessageId the message ID as it appears in the topic
     * @param topicId        the forum topic (thread) ID
     * @return the mapping holding the original user's chat ID and message ID, if found
     */
    Optional<MessageMapping> findByTopicMessageIdAndTopicId(Integer topicMessageId, Integer topicId);

    /**
     * Look up the mapping by the original user's chat ID and message ID.
     *
     * @param userChatId     the original user's chat ID (as a string, matched against the stored Long)
     * @param userMessageId  the original user's message ID
     * @return the mapping holding the topic message and topic IDs, if found
     */
    @Query("SELECT m FROM MessageMapping m WHERE m.userChatId = :userChatId AND m.userMessageId = :userMessageId")
    Optional<MessageMapping> findByUserChatIdAndUserMessageId(@Param("userChatId") String userChatId, @Param("userMessageId") int userMessageId);
}
