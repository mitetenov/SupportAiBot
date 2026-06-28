package com.vpnsupport.bot;

import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.util.List;

@Repository
public interface LlmTokenUsageRepository extends JpaRepository<LlmTokenUsage, Long> {

    @Query("SELECT new com.vpnsupport.bot.TokenStatsDto(u.telegramId, SUM(u.totalTokens), SUM(u.promptTokens), "
            + "SUM(u.completionTokens), COUNT(u)) "
            + "FROM LlmTokenUsage u GROUP BY u.telegramId "
            + "ORDER BY SUM(u.totalTokens) DESC")
    List<TokenStatsDto> findTopByTokens(Pageable pageable);

    @Query("SELECT new com.vpnsupport.bot.TokenStatsDto(u.telegramId, SUM(u.totalTokens), SUM(u.promptTokens), "
            + "SUM(u.completionTokens), COUNT(u)) "
            + "FROM LlmTokenUsage u WHERE u.telegramId = :telegramId "
            + "GROUP BY u.telegramId")
    List<TokenStatsDto> getStatsByTelegramId(@Param("telegramId") Long telegramId);
}
