// SPDX-License-Identifier: GPL-2.0 OR MIT
/* Modified for Salkim, 2026-10-29. See amdgpu/SOURCE for provenance. */
/*
 * Copyright 2023 Advanced Micro Devices, Inc.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the "Software"),
 * to deal in the Software without restriction, including without limitation
 * the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
 * THE COPYRIGHT HOLDER(S) OR AUTHOR(S) BE LIABLE FOR ANY CLAIM, DAMAGES OR
 * OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
 * ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
 * OTHER DEALINGS IN THE SOFTWARE.
 */

#include <linux/dma-fence.h>
#include <linux/dma-fence-unwrap.h>
#include <linux/workqueue.h>

#include "amdgpu.h"
#include "amdgpu_vm.h"
#include "amdgpu_gmc.h"

struct amdgpu_tlb_fence {
	struct dma_fence	base;
	struct amdgpu_device	*adev;
	struct dma_fence	*dependency;
	struct work_struct	work;
	spinlock_t		lock;
	uint16_t		pasid;

};

static const char *amdgpu_tlb_fence_get_driver_name(struct dma_fence *fence)
{
	return "amdgpu tlb fence";
}

static const char *amdgpu_tlb_fence_get_timeline_name(struct dma_fence *f)
{
	return "amdgpu tlb timeline";
}

static int amdgpu_tlb_fence_flush(struct amdgpu_device *adev, uint16_t pasid,
				  struct dma_fence *dependency)
{
	int r = 0, ret;

	if (dependency) {
		struct dma_fence_unwrap iter;
		struct dma_fence *fence;

		r = dma_fence_wait(dependency, false);
		if (r)
			return r;
		r = dma_fence_get_status(dependency);
		if (r > 0)
			r = 0;
		/* Polling an already completed array can hide a child error. */
		dma_fence_unwrap_for_each(fence, &iter, dependency) {
			ret = dma_fence_get_status(fence);
			if (ret < 0 && !r)
				r = ret;
		}
	}

	/* Failed updates may still have written part of the page tables. */
	ret = amdgpu_gmc_flush_gpu_tlb_pasid(adev, pasid, 2, true, 0);
	if (ret)
		dev_err(adev->dev, "TLB flush failed for PASID %d.\n", pasid);
	return r ?: ret;
}

static void amdgpu_tlb_fence_work(struct work_struct *work)
{
	struct amdgpu_tlb_fence *f = container_of(work, typeof(*f), work);
	int r;

	r = amdgpu_tlb_fence_flush(f->adev, f->pasid, f->dependency);
	dma_fence_put(f->dependency);
	f->dependency = NULL;
	if (r)
		dma_fence_set_error(&f->base, r);

	dma_fence_signal(&f->base);
	dma_fence_put(&f->base);
}

static const struct dma_fence_ops amdgpu_tlb_fence_ops = {
	.get_driver_name = amdgpu_tlb_fence_get_driver_name,
	.get_timeline_name = amdgpu_tlb_fence_get_timeline_name
};

/**
 * amdgpu_vm_tlb_fence_create - create a TLB invalidation fence
 * @adev: the amdgpu device
 * @vm: the vm to use
 * @dep: the dependency to wait for
 * @fence: the resulting fence
 *
 * Creates a TLB invalidation fence which signals when all the VMIDs assigned to
 * the PASID of the VM are invalidated.
 * Returns an error if synchronous invalidation fails. Asynchronous failures
 * are reported through the returned fence.
 */
int amdgpu_vm_tlb_fence_create(struct amdgpu_device *adev,
			       struct amdgpu_vm *vm,
			       struct dma_fence *dep,
			       struct dma_fence **fence)
{
	struct amdgpu_tlb_fence *f;
	int r;

	f = kmalloc_obj(*f);
	if (!f) {
		/* Preserve update ordering without an asynchronous fence. */
		r = amdgpu_tlb_fence_flush(adev, vm->pasid, dep);
		if (r)
			return r;

		dma_fence_put(*fence);
		*fence = dma_fence_get_stub();
		return 0;
	}

	f->adev = adev;
	f->dependency = dma_fence_get(dep);
	f->pasid = vm->pasid;
	INIT_WORK(&f->work, amdgpu_tlb_fence_work);
	spin_lock_init(&f->lock);

	dma_fence_init64(&f->base, &amdgpu_tlb_fence_ops, &f->lock,
			 vm->tlb_fence_context, atomic64_read(&vm->tlb_seq));

	/* TODO: We probably need a separate wq here */
	dma_fence_get(&f->base);
	schedule_work(&f->work);

	dma_fence_put(*fence);
	*fence = &f->base;
	return 0;
}
