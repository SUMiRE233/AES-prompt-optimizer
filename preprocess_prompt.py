"""
Prompt 预处理脚本
功能：将 _meta.md 文件转换为批改测试用的 prompt
"""

import os
import re
import glob


class PromptPreprocessor:
    def __init__(self):
        # =========================
        # 配置文件
        # =========================
        self.input_file = "origin_prompt_meta.md"
        self.output_file = "origin_prompt.md"
        self.sections_to_remove = [
            "## 评分标准",
            "## 待评作文",
            "## 输出格式",
        ]

    def remove_section_by_title(self, prompt_text, section_title):
        """根据标题移除整个 section 的内容"""
        pattern = rf'\n?{re.escape(section_title)}\n+.*?(?=\n## |\Z)'
        result = re.sub(pattern, '', prompt_text, flags=re.DOTALL)
        return result

    def remove_section_by_title_prefix(self, prompt_text, section_prefix):
        """根据标题前缀移除整个 section 的内容"""
        pattern = rf'\n?({re.escape(section_prefix)}\d*)\n+.*?(?=\n## |\Z)'
        result = re.sub(pattern, '', prompt_text, flags=re.DOTALL)
        return result

    def process(self, input_path=None, output_path=None):
        """处理单个文件"""
        if input_path is None:
            input_path = self.input_file
        if output_path is None:
            output_path = self.output_file
        
        print(f"读取文件: {input_path}")
        
        with open(input_path, 'r', encoding='utf-8') as f:
            prompt_text = f.read()
        
        original_length = len(prompt_text)
        print(f"原始长度: {original_length} 字符")
        
        processed_text = prompt_text
        
        for section in self.sections_to_remove:
            processed_text = self.remove_section_by_title(processed_text, section)
            processed_text = self.remove_section_by_title_prefix(processed_text, section)
        
        # 清理多余的空行
        processed_text = re.sub(r'\n{3,}', '\n\n', processed_text)
        processed_text = processed_text.strip()
        
        print(f"处理后长度: {len(processed_text)} 字符")
        print(f"移除内容: {original_length - len(processed_text)} 字符")
        
        remaining_sections = re.findall(r'(## [^\n]+)', processed_text)
        print(f"保留的 sections: {remaining_sections}")
        
        print(f"\n保存到: {output_path}")
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(processed_text)
        
        return processed_text

    def batch_process(self, pattern="*_meta.md"):
        """批量处理目录下所有 _meta.md 文件"""
        meta_files = glob.glob(os.path.join('.', pattern))
        
        if not meta_files:
            print(f"没有找到匹配 {pattern} 的文件")
            return
        
        print(f"找到 {len(meta_files)} 个文件\n")
        
        for meta_path in meta_files:
            print("=" * 60)
            output_path = meta_path.replace('_meta.md', '.md')
            self.process(meta_path, output_path)
            print()


if __name__ == "__main__":
    preprocessor = PromptPreprocessor()
    
    # 修改配置文件（根据需要修改）
    # preprocessor.input_file = "optimized_prompt1_meta.md"
    # preprocessor.output_file = "optimized_prompt1.md"
    # preprocessor.sections_to_remove = ["## 评分标准", "## 待评作文"]
    
    preprocessor.process()