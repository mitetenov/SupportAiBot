package telegram

import (
	"fmt"
	"html"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

var codeBlock = regexp.MustCompile("(?s)```(?:[a-zA-Z0-9_-]+)?\\n?(.*?)```")
var inlineCode = regexp.MustCompile("`([^`\\n]+)`")
var tagRE = regexp.MustCompile(`<(/?)([a-zA-Z0-9_-]+)(?:\s+[^>]*?)?>`)
var stripTagRE = regexp.MustCompile(`<[^>]+>`)

func escapeText(s string) string {
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;").Replace(s)
}
func MarkdownHTML(s string) string {
	if strings.TrimSpace(s) == "" {
		return s
	}
	saved := []string{}
	save := func(v string) string {
		key := fmt.Sprintf("§§MDGO%d§§", len(saved))
		saved = append(saved, v)
		return key
	}
	s = codeBlock.ReplaceAllStringFunc(s, func(m string) string {
		v := codeBlock.FindStringSubmatch(m)[1]
		return save("<pre><code>" + escapeText(strings.Trim(v, "\r\n")) + "</code></pre>")
	})
	s = inlineCode.ReplaceAllStringFunc(s, func(m string) string {
		return save("<code>" + escapeText(inlineCode.FindStringSubmatch(m)[1]) + "</code>")
	})
	s = escapeText(s)
	lines := strings.Split(s, "\n")
	var quoted bool
	for i, l := range lines {
		clean := strings.TrimLeft(l, " \t")
		if strings.HasPrefix(clean, "&gt;") {
			clean = strings.TrimPrefix(clean, "&gt;")
			clean = strings.TrimPrefix(clean, " ")
			if !quoted {
				clean = "<blockquote>" + clean
				quoted = true
			}
			lines[i] = clean
			if i+1 == len(lines) || !strings.HasPrefix(strings.TrimLeft(lines[i+1], " \t"), "&gt;") {
				lines[i] += "</blockquote>"
				quoted = false
			}
		}
	}
	s = strings.Join(lines, "\n")
	for _, item := range []struct{ pat, tag string }{{`(?s)\*\*(.+?)\*\*`, "b"}, {`(?s)__(.+?)__`, "b"}, {`(?s)~~(.+?)~~`, "s"}} {
		re := regexp.MustCompile(item.pat)
		s = re.ReplaceAllString(s, "<"+item.tag+">$1</"+item.tag+">")
	}
	for _, mark := range []byte{'*', '_'} {
		s = italic(s, mark)
	}
	link := regexp.MustCompile(`\[([^\]\n]+)\]\(((?:https?://|tg://)[^\)\s]+)\)`)
	s = link.ReplaceAllStringFunc(s, func(m string) string {
		v := link.FindStringSubmatch(m)
		return `<a href="` + strings.NewReplacer("&#34;", "&quot;", "&#39;", "&#x27;").Replace(html.EscapeString(v[2])) + `">` + v[1] + `</a>`
	})
	for i, v := range saved {
		s = strings.ReplaceAll(s, fmt.Sprintf("§§MDGO%d§§", i), v)
	}
	return s
}
func italic(s string, mark byte) string {
	r := []rune(s)
	var out strings.Builder
	for i := 0; i < len(r); {
		if r[i] != rune(mark) || (i > 0 && (unicode.IsLetter(r[i-1]) || unicode.IsDigit(r[i-1]) || r[i-1] == '_')) {
			out.WriteRune(r[i])
			i++
			continue
		}
		end := i + 1
		for end < len(r) && r[end] != rune(mark) && r[end] != '\n' {
			end++
		}
		if end > i+1 && end < len(r) && r[end] == rune(mark) && (end+1 == len(r) || (!unicode.IsLetter(r[end+1]) && !unicode.IsDigit(r[end+1]) && r[end+1] != '_')) {
			out.WriteString("<i>")
			out.WriteString(string(r[i+1 : end]))
			out.WriteString("</i>")
			i = end + 1
		} else {
			out.WriteRune(r[i])
			i++
		}
	}
	return out.String()
}
func StripHTML(s string) string { return html.UnescapeString(stripTagRE.ReplaceAllString(s, "")) }

type openTag struct{ name, full string }

func SplitHTML(s string, maxLen int) []string {
	if maxLen <= 0 {
		maxLen = 4096
	}
	if utf8.RuneCountInString(s) <= maxLen {
		return []string{s}
	}
	var chunks []string
	var tags []openTag
	current := ""
	prefix := func() string {
		v := ""
		for _, t := range tags {
			v += t.full
		}
		return v
	}
	suffix := func() string {
		v := ""
		for i := len(tags) - 1; i >= 0; i-- {
			v += "</" + tags[i].name + ">"
		}
		return v
	}
	flush := func() {
		if current != "" {
			chunks = append(chunks, current+suffix())
		}
		current = prefix()
	}
	for len(s) > 0 {
		loc := tagRE.FindStringSubmatchIndex(s)
		if loc != nil && loc[0] == 0 {
			full := s[:loc[1]]
			m := tagRE.FindStringSubmatch(full)
			extra := utf8.RuneCountInString(full)
			if m[1] == "" {
				extra += len(m[2]) + 3
			}
			if current != "" && utf8.RuneCountInString(current+suffix())+extra > maxLen {
				flush()
			}
			if m[1] == "" {
				tags = append(tags, openTag{strings.ToLower(m[2]), full})
			} else {
				for i := len(tags) - 1; i >= 0; i-- {
					if tags[i].name == strings.ToLower(m[2]) {
						tags = append(tags[:i], tags[i+1:]...)
						break
					}
				}
			}
			current += full
			s = s[loc[1]:]
			continue
		}
		end := len(s)
		if loc != nil {
			end = loc[0]
		}
		r := []rune(s[:end])
		s = s[end:]
		for len(r) > 0 {
			avail := maxLen - utf8.RuneCountInString(current+suffix())
			if avail <= 0 {
				flush()
				avail = maxLen - utf8.RuneCountInString(current+suffix())
			}
			if avail <= 0 {
				avail = 1
			}
			if len(r) <= avail {
				current += string(r)
				r = nil
				continue
			}
			cut := avail
			for i := avail - 1; i >= 0; i-- {
				if r[i] == '\n' {
					cut = i
					break
				}
			}
			if cut == avail {
				for i := avail - 1; i >= 0; i-- {
					if r[i] == ' ' {
						cut = i
						break
					}
				}
			}
			if cut == 0 {
				r = r[1:]
				flush()
				continue
			}
			current += string(r[:cut])
			if cut < avail {
				r = r[cut+1:]
			} else {
				r = r[cut:]
			}
			flush()
		}
	}
	if current != "" {
		chunks = append(chunks, current+suffix())
	}
	return chunks
}
