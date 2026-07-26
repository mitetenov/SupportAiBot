package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.model.PhotoSize;
import com.pengrad.telegrambot.request.GetFile;
import com.pengrad.telegrambot.response.GetFileResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

import java.util.Base64;

/** Downloads a Telegram photo and encodes it for the vision APIs. */
@Component
public class PhotoDownloader {

    private static final Logger log = LoggerFactory.getLogger(PhotoDownloader.class);
    private static final String DEFAULT_MIME_TYPE = "image/jpeg";

    private final TelegramBot telegramBot;
    private final WebClient webClient;

    public PhotoDownloader(TelegramBot telegramBot, WebClient webClient) {
        this.telegramBot = telegramBot;
        this.webClient = webClient;
    }

    /**
     * @return the downloaded photo, or a failure carrying the message-key to
     *         show the user
     */
    public Result download(PhotoSize[] photos) {
        if (photos == null || photos.length == 0) {
            return Result.failed("bot.photo.upload.error");
        }

        try {
            PhotoSize largest = photos[photos.length - 1];
            GetFileResponse fileResponse = telegramBot.execute(new GetFile(largest.fileId()));

            if (!fileResponse.isOk() || fileResponse.file() == null) {
                log.warn("Telegram getFile failed: {}", fileResponse.description());
                return Result.failed("bot.photo.upload.error");
            }

            var file = fileResponse.file();
            byte[] imageBytes = webClient.get()
                    .uri(telegramBot.getFullFilePath(file))
                    .retrieve()
                    .bodyToMono(byte[].class)
                    .block();

            if (imageBytes == null || imageBytes.length == 0) {
                return Result.failed("bot.photo.download.error");
            }

            return Result.ok(Base64.getEncoder().encodeToString(imageBytes),
                    detectMimeType(file.filePath()));
        } catch (Exception e) {
            log.error("Error downloading photo", e);
            return Result.failed("bot.photo.error");
        }
    }

    static String detectMimeType(String filePath) {
        if (filePath == null) {
            return DEFAULT_MIME_TYPE;
        }
        String lower = filePath.toLowerCase();
        if (lower.endsWith(".png")) return "image/png";
        if (lower.endsWith(".webp")) return "image/webp";
        if (lower.endsWith(".gif")) return "image/gif";
        return DEFAULT_MIME_TYPE;
    }

    public record Result(String base64Image, String mimeType, String errorMessageKey) {

        static Result ok(String base64Image, String mimeType) {
            return new Result(base64Image, mimeType, null);
        }

        static Result failed(String errorMessageKey) {
            return new Result(null, null, errorMessageKey);
        }

        public boolean isSuccess() {
            return base64Image != null;
        }
    }
}
