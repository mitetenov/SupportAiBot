package com.vpnsupport.rag;

public interface EmbeddingProvider {

    float[] embed(String text);

    int getDimension();
}
