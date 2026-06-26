package com.vpnsupport.bot;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

class UserEntityTest {

    @Test
    void shouldCreateWithDefaultConstructor() {
        UserEntity entity = new UserEntity();
        assertNull(entity.getTelegramId());
    }

    @Test
    void shouldCreateWithParameterizedConstructor() {
        UserEntity entity = new UserEntity(123L, "jdoe", "John", "Doe");
        assertEquals(123L, entity.getTelegramId());
        assertEquals("jdoe", entity.getUsername());
        assertEquals("John", entity.getFirstName());
        assertEquals("Doe", entity.getLastName());
        assertNotNull(entity.getUpdatedAt());
    }

    @Test
    void shouldSetAndGetAllFields() {
        UserEntity entity = new UserEntity();
        entity.setTelegramId(456L);
        entity.setUsername("janedoe");
        entity.setFirstName("Jane");
        entity.setLastName("Doe");
        entity.setUpdatedAt(java.time.LocalDateTime.of(2024, 1, 1, 12, 0));

        assertEquals(456L, entity.getTelegramId());
        assertEquals("janedoe", entity.getUsername());
        assertEquals("Jane", entity.getFirstName());
        assertEquals("Doe", entity.getLastName());
        assertEquals(java.time.LocalDateTime.of(2024, 1, 1, 12, 0), entity.getUpdatedAt());
    }

    @Test
    void shouldAllowNullFields() {
        UserEntity entity = new UserEntity();
        entity.setUsername(null);
        entity.setFirstName(null);
        entity.setLastName(null);

        assertNull(entity.getUsername());
        assertNull(entity.getFirstName());
        assertNull(entity.getLastName());
    }
}
