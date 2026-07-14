import re
from difflib import SequenceMatcher


def _char_ngrams(text, n=2):
    return [text[i:i+n] for i in range(len(text) - n + 1)]


def _tokenize(text):
    """中英文混合分词：英文按单词切分，中文按单字切分"""
    tokens = []
    for part in re.findall(r'[a-zA-Z]+|[一-鿿]', text):
        if part[0].isalpha():
            tokens.append(part.lower())
        else:
            tokens.extend(list(part))
    return tokens


def jaccard_similarity(set1, set2):
    if not set1 and not set2:
        return 1.0
    if not set1 or not set2:
        return 0.0
    intersection = set1 & set2
    union = set1 | set2
    return len(intersection) / len(union)


def ngram_similarity(text1, text2, n=2):
    """字符级 n-gram Jaccard 相似度，对中英文均有效"""
    ngrams1 = set(_char_ngrams(text1, n))
    ngrams2 = set(_char_ngrams(text2, n))
    return jaccard_similarity(ngrams1, ngrams2)


def token_overlap_similarity(text1, text2):
    """词/token 级 Jaccard 相似度"""
    tokens1 = set(_tokenize(text1))
    tokens2 = set(_tokenize(text2))
    return jaccard_similarity(tokens1, tokens2)


def sequence_similarity(text1, text2):
    """最长公共子序列比率（difflib.SequenceMatcher）"""
    return SequenceMatcher(None, text1, text2).ratio()


def length_ratio(text1, text2):
    """长度比：越接近1说明长度越相似，归一化到[0,1]"""
    if not text1 and not text2:
        return 1.0
    if not text1 or not text2:
        return 0.0
    shorter = min(len(text1), len(text2))
    longer = max(len(text1), len(text2))
    return shorter / longer


def semantic_similarity(text1: str, text2: str, threshold: float = 0.4) -> dict:
    """
    不依赖模型的语义相似度计算，综合多个信号加权打分。

    信号及权重：
      - token 重叠 (0.30)：词级 Jaccard，捕捉关键词匹配
      - 字符 n-gram (0.25)：2-gram Jaccard，捕捉局部字符模式
      - 序列匹配 (0.30)：LCS 比率，捕捉整体结构相似性
      - 长度比 (0.15)：惩罚长度差异过大的情况

    Args:
        text1: 文本1
        text2: 文本2
        threshold: 相似度阈值，>= 该值视为语义关联

    Returns:
        dict: {'similarity': float, 'is_related': bool, 'details': dict}
    """
    w_token, w_ngram, w_seq, w_len = 0.30, 0.25, 0.30, 0.15

    s_token = token_overlap_similarity(text1, text2)
    s_ngram = ngram_similarity(text1, text2, n=2)
    s_seq = sequence_similarity(text1, text2)
    s_len = length_ratio(text1, text2)

    sim = (w_token * s_token + w_ngram * s_ngram + w_seq * s_seq + w_len * s_len)

    return {
        'similarity': round(sim, 4),
        'is_related': sim >= threshold,
        'details': {
            'token_overlap': round(s_token, 4),
            'ngram': round(s_ngram, 4),
            'sequence': round(s_seq, 4),
            'length_ratio': round(s_len, 4),
        }
    }


if __name__ == '__main__':
    # 英文示例
    print(semantic_similarity('{"name": "search", "arguments": {"query": "weather today", "limit": 10}, "result": "sunny 25°C"}', '{"name": "search", "arguments": {"query": "weather today", "limit": 5}, "result": "sunny 25°C wind 3m/s"}'))
    print(semantic_similarity("maxResults: 100timeMax: '2025-06-30T23:59:59'ZtimeMin: '2025-06-26T00:00:00Z'", "maxResults: 25timeMax: '2025-06-30T23:59:59'ZtimeMin: '2025-06-26T00:00:00Z'"))
    print(semantic_similarity("He ran quickly", "The movement was fast"))

    # 中文示例
    print(semantic_similarity("Renault Clio 2001 engine cylinders 4 cylinder", "best selling car Argentina 2001"))
    print(semantic_similarity("查询用户订单信息", "获取用户的订单详情"))
    print(semantic_similarity("服务器返回错误", "接口调用成功返回数据"))