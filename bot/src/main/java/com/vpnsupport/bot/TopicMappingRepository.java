package com.vpnsupport.bot;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.util.Optional;

@Repository
public interface TopicMappingRepository extends JpaRepository<TopicMapping, Long> {
    Optional<TopicMapping> findByTopicId(Integer topicId);
}
