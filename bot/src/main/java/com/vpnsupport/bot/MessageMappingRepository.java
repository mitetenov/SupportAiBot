package com.vpnsupport.bot;

import org.springframework.data.jpa.repository.JpaRepository;
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
}
