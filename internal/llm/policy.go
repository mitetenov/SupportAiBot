package llm

import (
	"encoding/json"
	"strings"
	"unicode"
)

var rejections = []string{"не то", "не та", "не это", "не подходит", "не помог", "не помогло", "другой вариант", "другая инструкция", "нет, я про другое", "все равно не", "все еще не", "по-прежнему", "по прежнему", "ничего не изменилось", "ничего не поменялось", "ничего не помогает", "то же самое", "тоже самое", "опять не работает", "снова не работает", "также не работает", "так же не работает", "не заработало", "без изменений"}

func wordRune(r rune) bool { return unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_' }
func phrase(s, p string) bool {
	sr, pr := []rune(s), []rune(p)
	for i := 0; i+len(pr) <= len(sr); i++ {
		if string(sr[i:i+len(pr)]) == p && (i == 0 || !wordRune(sr[i-1])) && (i+len(pr) == len(sr) || !wordRune(sr[i+len(pr)])) {
			return true
		}
	}
	return false
}
func IsRejection(s string) bool {
	s = strings.ReplaceAll(strings.ToLower(s), "ё", "е")
	if strings.TrimRight(s, ".!?") == "другое" {
		return true
	}
	for _, p := range rejections {
		if phrase(s, p) {
			return true
		}
	}
	return false
}
func UserRequestsHuman(s string) bool {
	for _, word := range words(s) {
		if strings.HasPrefix(word, "оператор") || strings.HasPrefix(word, "человек") || strings.HasPrefix(word, "человеч") {
			return true
		}
		switch word {
		case "живой", "живого", "живому", "живым", "живом":
			return true
		}
	}
	return false
}
func ModelRequestedEscalation(s string) bool { return strings.Contains(s, "[ESCALATE]") }
func StripEscalationMarker(s string) string {
	return strings.TrimSpace(strings.ReplaceAll(s, "[ESCALATE]", ""))
}
func IsFollowUp(s string) bool {
	if IsRejection(s) {
		return true
	}
	letters := false
	for _, r := range s {
		if unicode.IsLetter(r) {
			letters = true
			break
		}
	}
	if !letters {
		return true
	}
	// Russian users routinely omit the diaeresis: these are the same
	// anaphoric references, and must retain the question they refer to.
	normalized := strings.ReplaceAll(strings.ToLower(s), "ё", "е")
	w := words(normalized)
	if len(w) == 0 {
		return true
	}
	lower := normalized
	switch w[0] {
	case "а", "и", "но", "ну":
		if strings.HasPrefix(lower, w[0]) {
			rest := []rune(strings.TrimPrefix(lower, w[0]))
			if len(rest) > 0 && unicode.IsSpace(rest[0]) {
				return true
			}
		}
	case "да", "нет", "ага", "угу", "ок", "окей", "понял", "поняла", "айфон", "iphone", "айпад", "ipad", "ios", "ipados", "андроид", "android", "винда", "windows", "макбук", "мак", "mac", "макос", "macos", "linux", "tv", "телевизор":
		if strings.HasPrefix(lower, w[0]) {
			return true
		}
	}
	for _, word := range w {
		switch word {
		case "это", "этот", "эта", "эту", "эти", "этим", "туда", "там", "тут", "оно", "его", "него", "ему", "нему", "им", "ее", "нее", "ей", "ею", "нем", "ней", "ним", "их", "них", "ими", "такое":
			return true
		}
	}
	return false
}
func jsonObject(s string) Object { out := Object{}; json.Unmarshal([]byte(s), &out); return out }
