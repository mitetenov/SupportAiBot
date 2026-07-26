package com.vpnsupport.bot;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class EscalationPolicyTest {

    @Test
    void shouldDetectTheModelMarker() {
        assertTrue(EscalationPolicy.modelRequestedEscalation("Оформим возврат. [ESCALATE]"));
        assertFalse(EscalationPolicy.modelRequestedEscalation("Обычный ответ"));
        assertFalse(EscalationPolicy.modelRequestedEscalation(null));
    }

    @Test
    void shouldStripTheMarkerAndSurroundingWhitespace() {
        assertEquals("Оформим возврат.", EscalationPolicy.stripMarker("Оформим возврат. [ESCALATE]"));
        assertEquals("", EscalationPolicy.stripMarker("[ESCALATE]"));
        assertEquals("", EscalationPolicy.stripMarker(null));
    }

    @Test
    void shouldDetectAnExplicitRequestForAPerson() {
        assertTrue(EscalationPolicy.userRequestsHuman("позовите оператора"));
        assertTrue(EscalationPolicy.userRequestsHuman("хочу поговорить с человеком"));
        assertTrue(EscalationPolicy.userRequestsHuman("дайте живого человека"));
        assertTrue(EscalationPolicy.userRequestsHuman("ОПЕРАТОР"));
    }

    @Test
    void shouldNotFireOnWordsThatMerelyContainATrigger() {
        // Each of these matched the old substring check on "жив"/"человек".
        assertFalse(EscalationPolicy.userRequestsHuman("я живу в Германии"));
        assertFalse(EscalationPolicy.userRequestsHuman("болит живот"));
        assertFalse(EscalationPolicy.userRequestsHuman("сайт оживает через раз"));
    }

    @Test
    void shouldHandleEmptyInput() {
        assertFalse(EscalationPolicy.userRequestsHuman(null));
        assertFalse(EscalationPolicy.userRequestsHuman(""));
        assertFalse(EscalationPolicy.userRequestsHuman("   "));
    }
}
