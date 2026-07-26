package com.vpnsupport.bot;

import com.pengrad.telegrambot.TelegramBot;
import com.pengrad.telegrambot.model.PhotoSize;
import com.pengrad.telegrambot.request.GetFile;
import com.pengrad.telegrambot.response.GetFileResponse;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.NullSource;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

import java.util.Base64;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class PhotoDownloaderTest {

    @Mock private TelegramBot telegramBot;
    @Mock private WebClient webClient;

    @SuppressWarnings({"unchecked", "rawtypes"})
    private void stubDownload(byte[] body) {
        WebClient.RequestHeadersUriSpec uriSpec = mock(WebClient.RequestHeadersUriSpec.class);
        WebClient.RequestHeadersSpec headersSpec = mock(WebClient.RequestHeadersSpec.class);
        WebClient.ResponseSpec responseSpec = mock(WebClient.ResponseSpec.class);

        lenient().when(webClient.get()).thenReturn(uriSpec);
        lenient().when(uriSpec.uri(anyString())).thenReturn(headersSpec);
        lenient().when(headersSpec.retrieve()).thenReturn(responseSpec);
        lenient().when(responseSpec.bodyToMono(byte[].class))
                .thenReturn(body == null ? Mono.empty() : Mono.just(body));
    }

    private void stubGetFile(boolean ok, String filePath) {
        GetFileResponse response = mock(GetFileResponse.class);
        lenient().when(response.isOk()).thenReturn(ok);
        if (ok) {
            com.pengrad.telegrambot.model.File file = mock(com.pengrad.telegrambot.model.File.class);
            lenient().when(file.filePath()).thenReturn(filePath);
            lenient().when(response.file()).thenReturn(file);
            lenient().when(telegramBot.getFullFilePath(file))
                    .thenReturn("https://api.telegram.org/file/bot/" + filePath);
        } else {
            lenient().when(response.file()).thenReturn(null);
            lenient().when(response.description()).thenReturn("FILE_NOT_FOUND");
        }
        lenient().when(telegramBot.execute(any(GetFile.class))).thenReturn(response);
    }

    private PhotoDownloader downloader() {
        return new PhotoDownloader(telegramBot, webClient);
    }

    private PhotoSize[] photos(int count) {
        PhotoSize[] sizes = new PhotoSize[count];
        for (int i = 0; i < count; i++) {
            PhotoSize size = mock(PhotoSize.class);
            lenient().when(size.fileId()).thenReturn("file-" + i);
            sizes[i] = size;
        }
        return sizes;
    }

    // ---------------------------------------------------------------- happy path

    @Test
    void shouldDownloadAndBase64EncodeThePhoto() {
        stubGetFile(true, "photos/img.jpg");
        stubDownload(new byte[]{1, 2, 3});

        PhotoDownloader.Result result = downloader().download(photos(1));

        assertTrue(result.isSuccess());
        assertEquals(Base64.getEncoder().encodeToString(new byte[]{1, 2, 3}), result.base64Image());
        assertEquals("image/jpeg", result.mimeType());
    }

    /**
     * Telegram sends the same photo in ascending sizes; the last entry is the
     * highest resolution and the only one worth showing the model.
     */
    @Test
    void shouldPickTheLargestSize() {
        stubGetFile(true, "photos/img.jpg");
        stubDownload(new byte[]{1});

        downloader().download(photos(4));

        ArgumentCaptor<GetFile> captor = ArgumentCaptor.forClass(GetFile.class);
        verify(telegramBot).execute(captor.capture());
        assertTrue(captor.getValue().getParameters().containsValue("file-3"),
                "expected the last (largest) PhotoSize to be requested, got "
                        + captor.getValue().getParameters());
    }

    // -------------------------------------------------------------- failure paths

    @Test
    void shouldReportAFailedGetFile() {
        stubGetFile(false, null);

        PhotoDownloader.Result result = downloader().download(photos(1));

        assertFalse(result.isSuccess());
        assertEquals("bot.photo.upload.error", result.errorMessageKey());
    }

    @Test
    void shouldReportAnEmptyDownloadBody() {
        stubGetFile(true, "photos/img.jpg");
        stubDownload(null);

        PhotoDownloader.Result result = downloader().download(photos(1));

        assertFalse(result.isSuccess());
        assertEquals("bot.photo.download.error", result.errorMessageKey());
    }

    @Test
    void shouldReportAZeroLengthDownload() {
        stubGetFile(true, "photos/img.jpg");
        stubDownload(new byte[0]);

        PhotoDownloader.Result result = downloader().download(photos(1));

        assertFalse(result.isSuccess());
        assertEquals("bot.photo.download.error", result.errorMessageKey());
    }

    @Test
    void shouldReportAnUnexpectedFailure() {
        when(telegramBot.execute(any(GetFile.class))).thenThrow(new RuntimeException("network down"));

        PhotoDownloader.Result result = downloader().download(photos(1));

        assertFalse(result.isSuccess());
        assertEquals("bot.photo.error", result.errorMessageKey());
    }

    @Test
    void shouldHandleAnEmptyPhotoArray() {
        assertFalse(downloader().download(new PhotoSize[0]).isSuccess());
        assertFalse(downloader().download(null).isSuccess());
    }

    // ------------------------------------------------------------------ mime type

    @ParameterizedTest
    @CsvSource({
            "photos/img.png,   image/png",
            "photos/img.PNG,   image/png",
            "photos/img.webp,  image/webp",
            "photos/img.gif,   image/gif",
            "photos/img.jpg,   image/jpeg",
            "photos/img.jpeg,  image/jpeg",
            "photos/no-suffix, image/jpeg"
    })
    void shouldDeriveTheMimeTypeFromTheFileSuffix(String path, String expected) {
        assertEquals(expected, PhotoDownloader.detectMimeType(path));
    }

    @ParameterizedTest
    @NullSource
    void shouldDefaultToJpegWithoutAPath(String path) {
        assertEquals("image/jpeg", PhotoDownloader.detectMimeType(path));
    }
}
