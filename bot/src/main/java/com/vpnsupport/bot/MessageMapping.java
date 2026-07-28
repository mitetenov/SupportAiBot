package com.vpnsupport.bot;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * Links a message in a support-group topic to its counterpart in the user's
 * private chat.
 *
 * <p>Both directions live in this table and are told apart by
 * {@link Direction}: a user message copied into the topic, and an operator
 * message delivered to the user. The pairing drives inline replies, reaction
 * forwarding and edit propagation.
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

    /** The message ID in the user's private chat with the bot. */
    @Column(name = "user_message_id", nullable = false)
    private Integer userMessageId;

    /**
     * Which way the message travelled. Nullable because rows written before
     * this column existed have no value; {@link #getDirection()} reads those as
     * {@link Direction#USER_TO_TOPIC}, which is what they all were.
     */
    @Enumerated(EnumType.STRING)
    @Column(name = "direction", length = 20)
    private Direction direction;

    /** Which side originated the message. */
    public enum Direction {
        /** A user message copied into the topic. */
        USER_TO_TOPIC,
        /** An operator message delivered to the user. */
        OPERATOR_TO_USER
    }

    public MessageMapping() {
    }

    public MessageMapping(Integer topicMessageId, Integer topicId, Long userChatId, Integer userMessageId) {
        this(topicMessageId, topicId, userChatId, userMessageId, Direction.USER_TO_TOPIC);
    }

    public MessageMapping(Integer topicMessageId, Integer topicId, Long userChatId,
                          Integer userMessageId, Direction direction) {
        this.topicMessageId = topicMessageId;
        this.topicId = topicId;
        this.userChatId = userChatId;
        this.userMessageId = userMessageId;
        this.direction = direction;
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

    /** Never null: legacy rows without a stored value were all user-to-topic. */
    public Direction getDirection() {
        return direction != null ? direction : Direction.USER_TO_TOPIC;
    }
}
