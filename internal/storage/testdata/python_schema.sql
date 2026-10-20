CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE bedolaga_ticket_state (
	ticket_id BIGINT NOT NULL,
	last_answered_message_id BIGINT NOT NULL,
	last_bot_reply_message_id BIGINT DEFAULT '0' NOT NULL,
	last_human_reply_message_id BIGINT DEFAULT '0' NOT NULL,
	last_mirrored_media_message_id BIGINT DEFAULT '0' NOT NULL,
	pending_media_message_id BIGINT DEFAULT '0' NOT NULL,
	last_human_reply_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (ticket_id)
)

;

CREATE TABLE chat_messages (
	id BIGSERIAL NOT NULL,
	telegram_id BIGINT NOT NULL,
	role VARCHAR(10) NOT NULL,
	content TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE faq (
	id VARCHAR(36) NOT NULL,
	question VARCHAR(2000) NOT NULL,
	answer TEXT NOT NULL,
	embedding VECTOR,
	keywords VARCHAR(2000),
	image VARCHAR(255),
	PRIMARY KEY (id)
)

;

CREATE TABLE faq_metadata (
	key VARCHAR(50) NOT NULL,
	val VARCHAR(256) NOT NULL,
	PRIMARY KEY (key)
)

;

CREATE TABLE knowledge_gaps (
	id BIGSERIAL NOT NULL,
	user_query VARCHAR(2000) NOT NULL,
	embedding VECTOR,
	best_faq_question VARCHAR(2000),
	max_similarity FLOAT,
	faq_count INTEGER,
	trigger_reason VARCHAR(20),
	bot_response VARCHAR(500),
	gap_count INTEGER NOT NULL,
	first_seen TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen TIMESTAMP WITH TIME ZONE NOT NULL,
	telegram_id BIGINT,
	PRIMARY KEY (id)
)

;

CREATE TABLE llm_token_usage (
	id BIGSERIAL NOT NULL,
	telegram_id BIGINT,
	prompt_tokens BIGINT,
	completion_tokens BIGINT,
	total_tokens BIGINT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE message_mappings (
	id BIGSERIAL NOT NULL,
	topic_message_id INTEGER NOT NULL,
	topic_id INTEGER NOT NULL,
	user_chat_id BIGINT NOT NULL,
	user_message_id INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)

;

CREATE TABLE topic_mappings (
	user_id BIGINT NOT NULL,
	topic_id INTEGER NOT NULL,
	user_name VARCHAR(255),
	active_ticket_id BIGINT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (user_id)
)

;

CREATE TABLE user_names (
	telegram_id BIGINT NOT NULL,
	username VARCHAR(255),
	first_name VARCHAR(255),
	last_name VARCHAR(255),
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (telegram_id)
)

;
