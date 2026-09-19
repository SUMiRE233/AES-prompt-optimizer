import json
import random


class EssayExtractor:
    def __init__(self):
        self.input_path = "origin_scoring_results.json"
        self.train_output_path = "train_essays.json"
        self.test_output_path = "test_essays.json"
        self.all_output_path = "all_essays.json"
        self.test_ratio = 0.25
        self.random_seed = 42
    
    def run(self, split=True):
        print("=" * 60)
        print("作文提取工具")
        print("=" * 60)
        
        print("\n[步骤1] 加载数据...")
        with open(self.input_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"  加载作文数: {len(data)}")
        
        print("\n[步骤2] 提取作文内容...")
        essays_with_meta = []
        for entry in data:
            essays_with_meta.append({
                "index": entry.get("index", 0),
                "name": entry.get("name", ""),
                "page": entry.get("page", ""),
                "essay": entry["essay"],
                "teacher": entry.get("teacher", {})
            })
        
        print("\n[步骤3] 保存全部作文...")
        all_essays = [{"index": item["index"], "essay": item["essay"]} for item in essays_with_meta]
        with open(self.all_output_path, 'w', encoding='utf-8') as f:
            json.dump(all_essays, f, ensure_ascii=False, indent=2)
        print(f"  保存成功: {self.all_output_path}")
        
        if split:
            print("\n[步骤4] 随机划分训练集和测试集...")
            print(f"  测试集比例: {self.test_ratio:.0%}")
            print(f"  随机种子: {self.random_seed}")
            
            random.seed(self.random_seed)
            shuffled = essays_with_meta.copy()
            random.shuffle(shuffled)
            
            test_size = int(len(shuffled) * self.test_ratio)
            test_data = shuffled[:test_size]
            train_data = shuffled[test_size:]
            
            train_essays = [{"index": item["index"], "essay": item["essay"]} for item in train_data]
            test_essays = [{"index": item["index"], "essay": item["essay"]} for item in test_data]
            
            with open(self.train_output_path, 'w', encoding='utf-8') as f:
                json.dump(train_essays, f, ensure_ascii=False, indent=2)
            print(f"  训练集保存成功: {self.train_output_path} ({len(train_essays)}篇)")
            
            with open(self.test_output_path, 'w', encoding='utf-8') as f:
                json.dump(test_essays, f, ensure_ascii=False, indent=2)
            print(f"  测试集保存成功: {self.test_output_path} ({len(test_essays)}篇)")
            
            train_meta_path = "train_meta.json"
            with open(train_meta_path, 'w', encoding='utf-8') as f:
                json.dump(train_data, f, ensure_ascii=False, indent=2)
            print(f"  训练集元数据保存成功: {train_meta_path}")
            
            test_meta_path = "test_meta.json"
            with open(test_meta_path, 'w', encoding='utf-8') as f:
                json.dump(test_data, f, ensure_ascii=False, indent=2)
            print(f"  测试集元数据保存成功: {test_meta_path}")
        
        print("\n" + "=" * 60)
        print("提取完成！")
        if split:
            print(f"  训练集: {len(train_essays)}篇")
            print(f"  测试集: {len(test_essays)}篇")
            print(f"  比例: {len(test_essays)}:{len(train_essays)}")
        else:
            print(f"  提取作文数: {len(all_essays)}")
            print(f"  输出文件: {self.all_output_path}")
        print("=" * 60)


if __name__ == "__main__":
    extractor = EssayExtractor()
    extractor.run()
