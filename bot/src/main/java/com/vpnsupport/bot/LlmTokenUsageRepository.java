package com.vpnsupport.bot;

import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.util.List;

@Repository
public interface LlmTokenUsageRepository extends JpaRepository<LlmTokenUsage, Long> {

    @Query("SELECT u.telegramId, SUM(u.totalTokens), SUM(u.promptTokens), "
            + "SUM(u.completionTokens), COUNT(u) "
            + "FROM LlmTokenUsage u GROUP BY u.telegramId "
            + "ORDER BY SUM(u.totalTokens) DESC")
    List<Object[]> findTopByTokens(Pageable pageable);

    @Query("SELECT SUM(u.totalTokens), SUM(u.promptTokens), SUM(u.completionTokens), COUNT(u) "
            + "FROM LlmTokenUsage u WHERE u.telegramId = :telegramId")
    List<Object[]> getStatsByTelegramId(@Param("telegramId") Long telegramId);
}
