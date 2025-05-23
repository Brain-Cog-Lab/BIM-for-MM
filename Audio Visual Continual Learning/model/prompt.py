import torch
import torch.nn as nn

class EPrompt(nn.Module):
    def __init__(self, length=4, key_dim=768, prompt_dim=768, embedding_key='mean', prompt_init='uniform', prompt_key=True,
                 pool_size=10, top_k=3, batchwise_prompt=False, prompt_key_init='uniform'):
        super().__init__()

        """
            self.prompt_list 用于后续取索引
            self.prompt 用于存储和修改值
        """
        self.length = length
        self.embedding_key = embedding_key
        self.prompt_init = prompt_init
        self.prompt_key = prompt_key
        self.pool_size = pool_size
        self.top_k = top_k
        self.batchwise_prompt = batchwise_prompt

        prompt_pool_shape = (pool_size, length * 2, prompt_dim)
        if prompt_init == 'zero':
            self.prompt = nn.Parameter(torch.zeros(prompt_pool_shape))
        elif prompt_init == 'uniform':
            self.prompt = nn.Parameter(torch.randn(prompt_pool_shape))
            nn.init.uniform_(self.prompt, -1, 1)

        # using learnable prompt keys
        key_shape = (pool_size, key_dim)
        if prompt_key_init == 'zero':
            self.prompt_key = nn.Parameter(torch.zeros(key_shape))
        elif prompt_key_init == 'uniform':
            self.prompt_key = nn.Parameter(torch.randn(key_shape))
            nn.init.uniform_(self.prompt_key, -1, 1)

            
    def l2_normalize(self, x, dim=None, epsilon=1e-12):
        """Normalizes a given vector or matrix."""
        square_sum = torch.sum(x ** 2, dim=dim, keepdim=True)
        x_inv_norm = torch.rsqrt(torch.maximum(square_sum, torch.tensor(epsilon, device=x.device)))
        return x * x_inv_norm

    
    def forward(self, x_embed, prompt_mask=None, cls_features=None):
        cls_features = cls_features.transpose(0,1).contiguous()  # (batch, seq, dim)
        x_embed = x_embed.transpose(0,1).contiguous()  # (batch, seq, dim)
        out = dict()
        if self.embedding_key == 'mean':
            x_embed_mean = torch.mean(x_embed, dim=1)
        elif self.embedding_key == 'max':
            x_embed_mean = torch.max(x_embed, dim=1)[0]
        elif self.embedding_key == 'mean_max':
            x_embed_mean = torch.max(x_embed, dim=1)[0] + 2 * torch.mean(x_embed, dim=1)
        elif self.embedding_key == 'cls':
            if cls_features is None:
                x_embed_mean = torch.max(x_embed, dim=1)[0] # B, C
            else:
                x_embed_mean = cls_features
        else:
            raise NotImplementedError("Not supported way of calculating embedding keys!")

        prompt_key_norm = self.l2_normalize(self.prompt_key, dim=-1) # Pool_size, C
        x_embed_norm = self.l2_normalize(x_embed_mean, dim=-1) # B, C

        similarity = torch.matmul(prompt_key_norm, x_embed_norm.t()) # pool_size, B or Pool_size, #class, B

        similarity = similarity.t() # B, pool_size

        (similarity_top_k, idx) = torch.topk(similarity, k=self.top_k, dim=1) # B, top_k
        out['similarity'] = similarity

        if self.batchwise_prompt:
            prompt_id, id_counts = torch.unique(idx, return_counts=True, sorted=True)
            # In jnp.unique, when the 'size' is specified and there are fewer than the indicated number of elements,
            # the remaining elements will be filled with 'fill_value', the default is the minimum value along the specified dimension.
            # Unless dimension is specified, this will be flattend if it is not already 1D.
            if prompt_id.shape[0] < self.pool_size:
                prompt_id = torch.cat([prompt_id, torch.full((self.pool_size - prompt_id.shape[0],), torch.min(idx.flatten()), device=prompt_id.device)])
                id_counts = torch.cat([id_counts, torch.full((self.pool_size - id_counts.shape[0],), 0, device=id_counts.device)])
            _, major_idx = torch.topk(id_counts, k=self.top_k) # top_k
            major_prompt_id = prompt_id[major_idx] # top_k
            # expand to batch
            idx = major_prompt_id.expand(x_embed.shape[0], -1).contiguous() # B, top_k

        if prompt_mask is not None:
            idx = prompt_mask # B, top_k

        out['prompt_idx'] = idx

        batched_prompt_raw = self.prompt[idx]  # B, top_k, length*2, C
        batch_size, top_k, length, c = batched_prompt_raw.shape
        key_prompt, value_prompt = batched_prompt_raw.split(length // 2, dim=2)

        out['key_prompt'] = key_prompt.reshape(batch_size, top_k * length // 2, c)
        out['value_prompt'] = value_prompt.reshape(batch_size, top_k * length // 2, c)

        batched_key_norm = prompt_key_norm[idx] # B, top_k, C

        out['selected_key'] = batched_key_norm
        out['prompt_key_norm'] = prompt_key_norm
        out['x_embed_norm'] = x_embed_norm

        # Put pull_constraint loss calculation inside
        x_embed_norm = x_embed_norm.unsqueeze(1) #  B, 1, C
        sim = batched_key_norm * x_embed_norm # B, top_k, C
        reduce_sim = torch.sum(sim) / (x_embed_norm.shape[0] * self.top_k) # Scalar

        out['reduce_sim'] = reduce_sim

        return out
