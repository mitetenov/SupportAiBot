package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.model.reaction.ReactionType;
import com.pengrad.telegrambot.model.reaction.ReactionTypeCustomEmoji;
import com.pengrad.telegrambot.model.reaction.ReactionTypeEmoji;
import com.pengrad.telegrambot.model.reaction.ReactionTypePaid;
import com.pengrad.telegrambot.request.SetMessageReaction;
import com.pengrad.telegrambot.response.BaseResponse;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullAndEmptySource;
import org.mockito.ArgumentCaptor;
import org.mockito.Captor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class TelegramMessageSenderTest {

    // ────────────────────────────
    // Existing split() tests
    // ────────────────────────────

    @ParameterizedTest
    @NullAndEmptySource
    void splitShouldReturnEmptyStringListForNullOrEmpty(String text) {
        List<String> result = TelegramMessageSender.split(text);
        assertEquals(1, result.size());
        assertEquals("", result.get(0));
    }

    @Test
    void splitShouldReturnSingleElementForShortText() {
        String text = "Hello, world!";
        List<String> result = TelegramMessageSender.split(text);
        assertEquals(1, result.size());
        assertEquals(text, result.get(0));
    }

    @Test
    void splitShouldReturnSingleElementForExactMaxLength() {
        String text = "A".repeat(4096);
        List<String> result = TelegramMessageSender.split(text);
        assertEquals(1, result.size());
        assertEquals(text, result.get(0));
    }

    @Test
    void splitShouldBreakAtNewlineWhenOverMaxLength() {
        StringBuilder sb = new StringBuilder();
        sb.append("A".repeat(4000));
        sb.append("\n");
        sb.append("B".repeat(200));
        String text = sb.toString();

        List<String> result = TelegramMessageSender.split(text);
        assertEquals(2, result.size());
        assertEquals(4000, result.get(0).length());
        assertEquals(200, result.get(1).length());
    }

    @Test
    void splitShouldHardBreakWhenNoNewline() {
        String text = "A".repeat(5000);
        List<String> result = TelegramMessageSender.split(text);
        assertEquals(2, result.size());
        assertEquals(4096, result.get(0).length());
        assertEquals(904, result.get(1).length());
    }

    @Test
    void splitShouldSkipLeadingNewlineBetweenChunks() {
        StringBuilder sb = new StringBuilder();
        sb.append("A".repeat(4000));
        sb.append("\n\n\n");
        sb.append("B".repeat(200));
        String text = sb.toString();

        List<String> result = TelegramMessageSender.split(text);
        assertEquals(2, result.size());
        assertEquals(4002, result.get(0).length());
        assertFalse(result.get(1).startsWith("\n"));
    }

    @Test
    void splitShouldHandleVeryLongText() {
        String text = "B".repeat(15000);
        List<String> result = TelegramMessageSender.split(text);
        int expectedChunks = (int) Math.ceil(15000.0 / 4096.0);
        assertEquals(expectedChunks, result.size());
        int totalLength = result.stream().mapToInt(String::length).sum();
        assertEquals(15000, totalLength);
    }

    @Test
    void splitShouldHandleTextWithTrailingNewline() {
        String text = "Hello\nWorld\n";
        List<String> result = TelegramMessageSender.split(text);
        assertEquals(1, result.size());
        assertEquals("Hello\nWorld\n", result.get(0));
    }

    @Test
    void splitShouldBreakOnLastNewlineWithinLimit() {
        StringBuilder sb = new StringBuilder();
        sb.append("A".repeat(4000));
        sb.append("\n");
        sb.append("B".repeat(200));
        String text = sb.toString();

        List<String> result = TelegramMessageSender.split(text);
        assertEquals(2, result.size());
    }

    @Test
    void splitShouldHandleTextWithNewlineAtMaxBoundary() {
        String part1 = "A".repeat(4095) + "\n";
        String part2 = "B".repeat(100);
        String text = part1 + part2;

        List<String> result = TelegramMessageSender.split(text);
        assertEquals(2, result.size());
        assertEquals(4095, result.get(0).length());
        assertEquals(100, result.get(1).length());
    }

    // ────────────────────────────
    // setReaction() tests
    // ────────────────────────────

    @Mock
    private TelegramBot telegramBot;

    @Captor
    private ArgumentCaptor<SetMessageReaction> requestCaptor;

    private TelegramMessageSender createSender() {
        return new TelegramMessageSender(telegramBot);
    }

    private BaseResponse okResponse() {
        BaseResponse response = mock(BaseResponse.class);
        when(response.isOk()).thenReturn(true);
        return response;
    }

    private BaseResponse failResponse() {
        BaseResponse response = mock(BaseResponse.class);
        when(response.isOk()).thenReturn(false);
        when(response.description()).thenReturn("MESSAGE_ID_INVALID");
        when(response.errorCode()).thenReturn(400);
        return response;
    }

    @Test
    void shouldSetEmojiReaction() {
        BaseResponse ok = okResponse();
        when(telegramBot.execute(any(SetMessageReaction.class))).thenReturn(ok);

        TelegramMessageSender sender = createSender();
        ReactionTypeEmoji reaction = new ReactionTypeEmoji("👍");
        sender.setReaction("-100123456789", 42, List.of(reaction));

        verify(telegramBot).execute(requestCaptor.capture());
        SetMessageReaction request = requestCaptor.getValue();
        assertNotNull(request);
    }

    @Test
    void shouldSetCustomEmojiReaction() {
        BaseResponse ok = okResponse();
        when(telegramBot.execute(any(SetMessageReaction.class))).thenReturn(ok);

        TelegramMessageSender sender = createSender();
        ReactionTypeCustomEmoji reaction = new ReactionTypeCustomEmoji("custom_emoji_id_123");
        sender.setReaction("-100123456789", 42, List.of(reaction));

        verify(telegramBot).execute(any(SetMessageReaction.class));
    }

    @Test
    void shouldSetPaidReaction() {
        BaseResponse ok = okResponse();
        when(telegramBot.execute(any(SetMessageReaction.class))).thenReturn(ok);

        TelegramMessageSender sender = createSender();
        sender.setReaction("-100123456789", 42, List.of(new ReactionTypePaid()));

        verify(telegramBot).execute(any(SetMessageReaction.class));
    }

    @Test
    void shouldSetMultipleReactions() {
        BaseResponse ok = okResponse();
        when(telegramBot.execute(any(SetMessageReaction.class))).thenReturn(ok);

        TelegramMessageSender sender = createSender();
        List<ReactionType> reactions = List.of(
                new ReactionTypeEmoji("👍"),
                new ReactionTypeCustomEmoji("custom_123"),
                new ReactionTypePaid()
        );
        sender.setReaction("-100123456789", 42, reactions);

        verify(telegramBot).execute(any(SetMessageReaction.class));
    }

    @Test
    void shouldRemoveReactionsWhenEmptyList() {
        BaseResponse ok = okResponse();
        when(telegramBot.execute(any(SetMessageReaction.class))).thenReturn(ok);

        TelegramMessageSender sender = createSender();
        sender.setReaction("-100123456789", 42, List.of());

        verify(telegramBot).execute(requestCaptor.capture());
        SetMessageReaction request = requestCaptor.getValue();
        assertNotNull(request);
    }

    @Test
    void shouldRemoveReactionsWhenNull() {
        BaseResponse ok = okResponse();
        when(telegramBot.execute(any(SetMessageReaction.class))).thenReturn(ok);

        TelegramMessageSender sender = createSender();
        sender.setReaction("-100123456789", 42, null);

        verify(telegramBot).execute(any(SetMessageReaction.class));
    }

    @Test
    void shouldLogErrorWhenApiFails() {
        BaseResponse fail = failResponse();
        when(telegramBot.execute(any(SetMessageReaction.class))).thenReturn(fail);

        TelegramMessageSender sender = createSender();
        sender.setReaction("-100123456789", 42, List.of(new ReactionTypeEmoji("👍")));

        verify(telegramBot).execute(any(SetMessageReaction.class));
    }

    @Test
    void shouldHandleExceptionGracefully() {
        when(telegramBot.execute(any(SetMessageReaction.class)))
                .thenThrow(new RuntimeException("Network error"));

        TelegramMessageSender sender = createSender();
        sender.setReaction("-100123456789", 42, List.of(new ReactionTypeEmoji("👍")));

        verify(telegramBot).execute(any(SetMessageReaction.class));
    }
}
