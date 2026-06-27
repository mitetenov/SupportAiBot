package com.vpnsupport.bot;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * Maps a message ID in a support-group topic thread back to the original
 * user's chat ID and message ID. Used to forward operator replies as
 * inline replies to the original user message.
 */
@Entity
@Table(name = "message_mappings")
public class MessageMapping {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    /** ID of the message as it appears in the topic thread. */
    @Column(name = "topic_message_id", nullable = false)
    private Integer topicMessageId;

    /** The forum topic (thread) ID where the copied message lives. */
    @Column(name = "topic_id", nullable = false)
    private Integer topicId;

    /** The original user's chat ID. */
    @Column(name = "user_chat_id", nullable = false)
    private Long userChatId;

    /** The original user's message ID in their private chat with the bot. */
    @Column(name = "user_message_id", nullable = false)
    private Integer userMessageId;

    public MessageMapping() {
    }

    public MessageMapping(Integer topicMessageId, Integer topicId, Long userChatId, Integer userMessageId) {
        this.topicMessageId = topicMessageId;
        this.topicId = topicId;
        this.userChatId = userChatId;
        this.userMessageId = userMessageId;
    }

    public Long getId() {
        return id;
    }

    public Integer getTopicMessageId() {
        return topicMessageId;
    }

    public Integer getTopicId() {
        return topicId;
    }

    public Long getUserChatId() {
        return userChatId;
    }

    public Integer getUserMessageId() {
        return userMessageId;
    }
}
